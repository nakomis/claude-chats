"""
Claude Code Stop hook.

Reads the session transcript and appends new messages to a local SQLite
outbox. A separate forwarder daemon drains that outbox to the ingest queue,
and a consumer inside the house does the embedding and the database write.

This hook deliberately does *no* network I/O, holds no credentials, and never
talks to Postgres. That is the whole point: capture must not be able to fail.

It used to write straight to Postgres, and it failed silently — when the Docker
daemon holding the local database was down, the write threw and the message
vanished with no signal at all. Pointing it at a remote database would have
made that worse, not better: more failure modes (network, credentials, being
away from home), not fewer.

So capture and delivery are split, and only capture has to be reliable:

  * this hook appends to a local SQLite file — no network, no auth, no daemon,
    nothing that can be "down";
  * the forwarder owns everything that *can* fail, and when it does the outbox
    simply grows and drains later.

Always exits 0 so Claude is never blocked from stopping. Failures are reported
on stderr and fall back to a plain append-only file, rather than being swallowed.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
from datetime import datetime, timezone

OUTBOX_PATH = os.environ.get(
    "CLAUDE_CHATS_OUTBOX",
    os.path.expanduser("~/.claude-chats/outbox.db"),
)
# Derived from the outbox rather than hardcoded, so overriding the outbox
# location doesn't leave the fallback writing somewhere unrelated.
FALLBACK_PATH = os.path.join(os.path.dirname(OUTBOX_PATH), "outbox-fallback.ndjson")
# A friendly label ("work-laptop") beats a raw hostname, which changes with
# whatever the DHCP lease felt like that day. Several machines share one
# database, so search can span every tenant or filter to one.
HOST = os.environ.get("CLAUDE_CHATS_HOST") or socket.gethostname()

# Schema shared with the Rust forwarder (mac/conversation-memory-forwarder in
# nakomis/home-infra). Whichever process opens the file first creates it, so
# neither depends on the other having been installed. KEEP THE TWO IN STEP:
# tests/test_outbox_schema.py pins a checksum of this DDL, and the forwarder's
# outbox.rs pins the identical value — change one copy and both test suites go
# red until the other copy and both hashes are updated together (HOME-194).
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS outbox (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_uuid      TEXT    NOT NULL UNIQUE,
    session_id        TEXT    NOT NULL,
    project_path      TEXT    NOT NULL DEFAULT '',
    git_branch        TEXT    NOT NULL DEFAULT '',
    conversation_name TEXT,
    host              TEXT,
    role              TEXT    NOT NULL,
    content           TEXT    NOT NULL,
    sequence_num      INTEGER NOT NULL,
    created_at        TEXT    NOT NULL,
    sent_at           TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox (sent_at, id);

CREATE TABLE IF NOT EXISTS forwarder_state (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    """Extract plain text from a message content value.

    Content may be a plain string or a list of content blocks
    (text / tool_use / tool_result / …).
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for ib in inner:
                        if isinstance(ib, dict) and ib.get("type") == "text":
                            parts.append(ib.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return ""


# Claude Code records a pasted image's origin in a separate `isMeta` entry
# whose parentUuid points back at the message carrying the image block:
#
#   message  uuid=c497a6cb  content=[text "[Image #1]", image(base64)]
#   meta     parentUuid=c497a6cb  text="[Image: source: /var/folders/…/Foo.png]"
#
# The image block itself has no filename, and the message's own text is only
# "[Image #1]", so this meta entry is the ONLY place the original name survives.
# Images pasted from the clipboard have no source file and therefore no meta
# entry at all — in one real transcript, 59 meta entries against 63 image
# blocks — so the name must always be treated as optional.
_IMAGE_SOURCE_RE = re.compile(r"^\[Image:\s*source:\s*(.+?)\]\s*$")


def _image_sources(entries: list[dict]) -> dict[str, list[str]]:
    """Map a message uuid -> the source paths of the images it carries.

    Order is preserved so the Nth path lines up with the Nth image block in
    the parent message.
    """
    sources: dict[str, list[str]] = {}
    for entry in entries:
        if not entry.get("isMeta"):
            continue
        parent = entry.get("parentUuid")
        if not parent:
            continue
        content = entry.get("message", {}).get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            match = _IMAGE_SOURCE_RE.match(block.get("text", "").strip())
            if match:
                sources.setdefault(parent, []).append(match.group(1))
    return sources


def _image_markers(content, source_paths: list[str]) -> list[str]:
    """Render a text stand-in for each image block, in block order.

    Claude Code always writes a "[Image #N]" text block alongside the image, so
    these messages do reach the outbox — but that placeholder is the entirety of
    what gets stored. On recall you can see that an image was sent and have no
    idea what it was, nor any way to reach it (HOME-315).

    The marker carries the sha256 prefix of the DECODED bytes, which is the same
    join key the image archive uses for the file and its sidecar (HOME-298), so
    a search hit resolves to the actual picture rather than merely proving one
    existed. Hash the decoded bytes, not the base64 text: the archive writes
    decoded bytes, and the two must agree.
    """
    if not isinstance(content, list):
        return []
    markers: list[str] = []
    seen = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source", {})
        media_type = source.get("media_type") or "image"
        digest = ""
        if source.get("type") == "base64" and source.get("data"):
            try:
                digest = hashlib.sha256(
                    base64.b64decode(source["data"], validate=True)
                ).hexdigest()[:8]
            except (binascii.Error, ValueError):
                # A corrupt payload must not cost us the whole message.
                digest = ""
        name = ""
        if seen < len(source_paths):
            name = os.path.basename(source_paths[seen])
        seen += 1

        bits = [b for b in (media_type, f"sha256:{digest}" if digest else "") if b]
        markers.append(f"[image: {name or 'pasted'} ({', '.join(bits)})]")
    return markers


def _write_fallback(records: list[dict], reason: str) -> None:
    """Last-resort capture when SQLite itself is unavailable.

    A plain append to a text file has fewer moving parts than anything else we
    could reach for, so it is what stands between a broken outbox and a lost
    conversation. Recover with scripts/replay-fallback.py.
    """
    path = FALLBACK_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        print(
            f"claude-chats: outbox unavailable ({reason}); "
            f"wrote {len(records)} message(s) to {path}",
            file=sys.stderr,
        )
    except Exception as exc:  # pragma: no cover - the truly hopeless case
        print(
            f"claude-chats: LOST {len(records)} message(s) — "
            f"outbox failed ({reason}) and fallback failed ({exc})",
            file=sys.stderr,
        )


def _append_to_outbox(records: list[dict], session_id: str, name: str | None) -> None:
    conn = sqlite3.connect(OUTBOX_PATH, timeout=10)
    try:
        conn.executescript(SCHEMA)
        with conn:
            # INSERT OR IGNORE against the UNIQUE message_uuid is what makes
            # this safe to run on every Stop: the hook re-reads the entire
            # transcript each time, and rows already forwarded remain as
            # tombstones (content blanked, sent_at set) precisely so they still
            # suppress a re-insert here.
            conn.executemany(
                """
                INSERT OR IGNORE INTO outbox
                    (message_uuid, session_id, project_path, git_branch,
                     conversation_name, host, role, content, sequence_num, created_at)
                VALUES
                    (:message_uuid, :session_id, :project_path, :git_branch,
                     :conversation_name, :host, :role, :content, :sequence_num, :created_at)
                """,
                records,
            )
            if name:
                # A rename can land after the messages it applies to. Update
                # anything still pending so the new name travels with it.
                conn.execute(
                    "UPDATE outbox SET conversation_name = ? "
                    "WHERE session_id = ? AND sent_at IS NULL",
                    (name, session_id),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id      = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd             = payload.get("cwd", "")
    git_branch      = payload.get("git_branch", "")

    if not session_id or not transcript_path:
        sys.exit(0)

    transcript_path = os.path.expanduser(transcript_path)

    try:
        with open(transcript_path) as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
    except Exception:
        sys.exit(0)

    messages = [
        e for e in entries
        if isinstance(e.get("message"), dict)
        and e["message"].get("role") in ("user", "assistant")
    ]

    # Extract the most recent /rename value if present
    name: str | None = None
    for entry in reversed(entries):
        content = entry.get("content", "")
        if (
            entry.get("type") == "system"
            and entry.get("subtype") == "local_command"
            and isinstance(content, str)
            and "<command-name>/rename</command-name>" in content
        ):
            args_start = content.find("<command-args>") + len("<command-args>")
            args_end = content.find("</command-args>")
            if args_start > -1 and args_end > -1:
                name = content[args_start:args_end].strip() or None
            break

    if not messages and name is None:
        sys.exit(0)

    # uuid -> source paths of the images that message carries (HOME-315).
    image_sources = _image_sources(entries)

    records = []
    for seq, entry in enumerate(messages):
        msg  = entry["message"]
        content = msg.get("content", "")
        text = _extract_text(content)
        markers = _image_markers(content, image_sources.get(entry.get("uuid") or "", []))
        if markers:
            # Appended, not substituted: "[Image #1]" carries the ordering the
            # rest of the conversation refers to, so keep it and add the detail.
            text = "\n".join(part for part in (text, *markers) if part)
        if not text:
            continue
        ts_raw = entry.get("timestamp")
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(timezone.utc)
        records.append({
            "message_uuid":      entry.get("uuid") or f"{session_id}:{seq}",
            "session_id":        session_id,
            "project_path":      cwd,
            "git_branch":        git_branch,
            "conversation_name": name,
            "host":              HOST,
            "role":              msg["role"],
            "content":           text,
            "sequence_num":      seq,
            # The real transcript timestamp. The forwarder may not deliver this
            # for days if we are offline, so it must not be re-derived later.
            "created_at":        ts.isoformat(),
        })

    if not records and name is None:
        sys.exit(0)

    try:
        os.makedirs(os.path.dirname(OUTBOX_PATH), exist_ok=True)
        _append_to_outbox(records, session_id, name)
    except Exception as exc:
        # Never swallow this. A silent failure here is exactly the bug this
        # design exists to remove — but still exit 0, because blocking Claude
        # from stopping would be a worse outcome than a delayed message.
        _write_fallback(records, str(exc))

    sys.exit(0)


if __name__ == "__main__":
    main()
