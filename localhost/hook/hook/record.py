"""
Claude Code hook — single-machine edition.

Reads the session transcript and records new messages. There are two capture
modes, chosen at install time and switchable by re-running install.sh:

  direct   The hook embeds each message and writes it to Postgres inline.
           Fewest moving parts, and a message is searchable the moment the
           session stops. The cost is that capture now depends on Docker,
           Postgres and the embedding provider all being up.

  durable  The hook appends to a local SQLite outbox and does no network I/O
           at all — no credentials, no daemon, nothing that can be "down".
           `drain-outbox` embeds and writes to Postgres afterwards, so a
           stopped database becomes a delay rather than a lost message.

The durable mode exists because direct mode was the original design and it
failed silently: when the Docker daemon holding the database was down, the write
threw and the message vanished with no signal at all. Both modes therefore fall
back to an append-only NDJSON file when their write fails, rather than swallowing
the error — recover it with `install.sh --replay-fallback`.

Always exits 0 so Claude is never blocked from stopping.
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

# direct | durable. Set by install.sh on the hook command line; the default is
# the safer of the two, so a hand-rolled invocation cannot silently lose data.
CAPTURE_MODE = os.environ.get("CLAUDE_CHATS_CAPTURE_MODE", "durable").strip().lower()

DB_URL = os.environ.get(
    "CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats"
)
OUTBOX_PATH = os.environ.get(
    "CLAUDE_CHATS_OUTBOX",
    os.path.expanduser("~/.claude-chats/outbox.db"),
)
# Derived from the outbox rather than hardcoded, so overriding the outbox
# location doesn't leave the fallback writing somewhere unrelated.
FALLBACK_PATH = os.path.join(os.path.dirname(OUTBOX_PATH), "outbox-fallback.ndjson")
# A friendly label ("work-laptop") beats a raw hostname, which changes with
# whatever the DHCP lease felt like that day. Single-machine only ever writes
# one value, but the column is shared with the distributed schema.
def _default_host() -> str:
    """This machine's hostname, minus any ``.local`` suffix.

    macOS reports ``phi`` on some networks and ``phi.local`` on others, which
    split one machine's history across two labels in the database (HOME-395).
    """
    name = socket.gethostname()
    return name[: -len(".local")] if name.endswith(".local") else name


HOST = os.environ.get("CLAUDE_CHATS_HOST") or _default_host()

# Kept byte-identical to the distributed edition's outbox DDL so an outbox
# written by one can be drained by the other. Only durable mode touches it.
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
    ai_title          TEXT,
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

def _vec(embedding: list[float]) -> str:
    """pgvector's text input format."""
    return "[" + ",".join(str(x) for x in embedding) + "]"


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
    """Last-resort capture when the primary write fails.

    That is Postgres in direct mode and SQLite in durable mode. A plain append to
    a text file has fewer moving parts than anything else we could reach for, so
    it is what stands between either of those being broken and a lost
    conversation. Recover with `install.sh --replay-fallback`.
    """
    path = FALLBACK_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        # Name the thing that actually broke: in direct mode it is Postgres,
        # in durable mode the outbox, and "outbox unavailable" against a stopped
        # database sends you looking in the wrong place.
        what = "database" if CAPTURE_MODE == "direct" else "outbox"
        print(
            f"claude-chats: {what} unavailable ({reason}); "
            f"wrote {len(records)} message(s) to {path}",
            file=sys.stderr,
        )
    except Exception as exc:  # pragma: no cover - the truly hopeless case
        print(
            f"claude-chats: LOST {len(records)} message(s) — "
            f"primary write failed ({reason}) and fallback failed ({exc})",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Session titles
# ---------------------------------------------------------------------------

def _sidecar_title(transcript_path: str, session_id: str) -> str | None:
    """The name from ``<session-id>/custom-title.json`` beside the transcript.

    Located from the transcript's own directory rather than from cwd, which
    drifts whenever a session changes directory or enters a worktree.
    """
    path = os.path.join(os.path.dirname(transcript_path), session_id, "custom-title.json")
    try:
        with open(path) as fh:
            title = json.load(fh).get("customTitle")
    except Exception:
        return None
    return (title.strip() or None) if isinstance(title, str) else None


def _session_name(entries: list[dict], transcript_path: str, session_id: str) -> str | None:
    """The name Martin gave the session with /rename, or None.

    Claude Code has stored this three ways (HOME-391), and the hook broke
    silently when it moved from the first to the others:

      * the ``custom-title.json`` sidecar — one current value, so checked first;
      * ``{"type": "custom-title"}`` transcript entries, re-appended over the
        session's life — the last one is current;
      * a ``local_command`` entry wrapping ``<command-name>/rename`` — the old
        format, kept so replaying old transcripts still finds their names.
    """
    name = _sidecar_title(transcript_path, session_id)
    if name:
        return name
    for entry in reversed(entries):
        if entry.get("type") == "custom-title":
            title = entry.get("customTitle")
            if isinstance(title, str) and title.strip():
                return title.strip()
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
                return content[args_start:args_end].strip() or None
            return None
    return None


def _ai_title(entries: list[dict]) -> str | None:
    """Claude Code's generated title for the session, or None.

    Kept apart from the name on purpose: a name is what Martin chose, and this
    is a guess. Claude Code rewrites it as the conversation develops, so the
    last entry wins.
    """
    for entry in reversed(entries):
        if entry.get("type") == "ai-title":
            title = entry.get("aiTitle")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return None


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after an outbox was created.

    `CREATE TABLE IF NOT EXISTS` is a no-op against an existing file, so without
    this an outbox from before HOME-391 would fail every INSERT on ai_title.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    if have and "ai_title" not in have:
        conn.execute("ALTER TABLE outbox ADD COLUMN ai_title TEXT")


def _append_to_outbox(
    records: list[dict],
    session_id: str,
    name: str | None,
    ai_title: str | None = None,
) -> None:
    conn = sqlite3.connect(OUTBOX_PATH, timeout=10)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        with conn:
            # INSERT OR IGNORE against the UNIQUE message_uuid is what makes
            # this safe to run on every Stop: the hook re-reads the entire
            # transcript each time, and rows already drained remain as
            # tombstones (content blanked, sent_at set) precisely so they still
            # suppress a re-insert here.
            conn.executemany(
                """
                INSERT OR IGNORE INTO outbox
                    (message_uuid, session_id, project_path, git_branch,
                     conversation_name, ai_title, host, role, content,
                     sequence_num, created_at)
                VALUES
                    (:message_uuid, :session_id, :project_path, :git_branch,
                     :conversation_name, :ai_title, :host, :role, :content,
                     :sequence_num, :created_at)
                """,
                records,
            )
            if name or ai_title:
                _carry_titles(conn, records, session_id, name, ai_title)
    finally:
        conn.close()


def _carry_titles(
    conn: sqlite3.Connection,
    records: list[dict],
    session_id: str,
    name: str | None,
    ai_title: str | None,
) -> None:
    """Make sure the session's current titles reach the database.

    Titles only travel on message rows. A rename can land after the messages it
    applies to, so anything still pending is updated to carry it. COALESCE, so
    a run that found no title never blanks one an earlier run stored.
    """
    pending = conn.execute(
        "UPDATE outbox SET conversation_name = COALESCE(?, conversation_name), "
        "ai_title = COALESCE(?, ai_title) "
        "WHERE session_id = ? AND sent_at IS NULL",
        (name, ai_title, session_id),
    ).rowcount
    if pending:
        return

    # Nothing pending, so the new title has nothing to ride on: typically a
    # /rename as the last act of a session, caught by the SessionEnd hook
    # (HOME-391). Re-queue the newest row if what it last carried is stale.
    latest = conn.execute(
        "SELECT id, message_uuid, conversation_name, ai_title FROM outbox "
        "WHERE session_id = ? ORDER BY sequence_num DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if latest is None:
        return
    row_id, message_uuid, sent_name, sent_ai_title = latest
    stale = (name and name != sent_name) or (ai_title and ai_title != sent_ai_title)
    if not stale:
        return
    # The row is a tombstone with its content blanked, and the consumer's
    # upsert overwrites content — so resending it as-is would erase the message
    # in Postgres. Refill it from the transcript, or leave it alone.
    record = next((r for r in records if r["message_uuid"] == message_uuid), None)
    if record is None:
        return
    conn.execute(
        "UPDATE outbox SET sent_at = NULL, attempts = 0, last_error = NULL, "
        "content = ?, conversation_name = COALESCE(?, conversation_name), "
        "ai_title = COALESCE(?, ai_title) WHERE id = ?",
        (record["content"], name, ai_title, row_id),
    )


# ---------------------------------------------------------------------------
# Direct mode — embed and write to Postgres inline
# ---------------------------------------------------------------------------

def _write_to_postgres(records: list[dict], session_id: str, project_path: str,
                       git_branch: str, name: str | None,
                       ai_title: str | None = None) -> None:
    """Upsert the conversation and insert any messages it does not already have.

    Deduplication is by message_uuid read back from the database, because the
    hook re-reads the whole transcript on every Stop and would otherwise
    re-embed and re-insert entire conversations.
    """
    from hook.embed import get_embedding
    import psycopg

    with psycopg.connect(DB_URL, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversations (session_id, project_path, git_branch, host)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id) DO NOTHING
                """,
                (session_id, project_path, git_branch, HOST),
            )
            if name or ai_title:
                # Direct mode writes on every hook run, SessionEnd included, so
                # a late rename lands here with no outbox revival needed.
                cur.execute(
                    "UPDATE conversations SET name = COALESCE(%s, name), "
                    "ai_title = COALESCE(%s, ai_title) WHERE session_id = %s",
                    (name, ai_title, session_id),
                )
            cur.execute(
                "SELECT id FROM conversations WHERE session_id = %s", (session_id,)
            )
            conv_id = cur.fetchone()[0]

            cur.execute(
                "SELECT message_uuid FROM messages WHERE conversation_id = %s",
                (conv_id,),
            )
            stored = {row[0] for row in cur.fetchall()}

            for item in records:
                if item["message_uuid"] in stored:
                    continue
                try:
                    embedding = get_embedding(item["content"])
                except Exception:
                    # A provider that is down costs the vector, not the message.
                    # `install.sh --backfill` fills these in later.
                    embedding = None

                if embedding:
                    cur.execute(
                        """
                        INSERT INTO messages
                            (conversation_id, message_uuid, role, content,
                             embedding, created_at, sequence_num)
                        VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
                        ON CONFLICT (message_uuid) DO NOTHING
                        """,
                        (conv_id, item["message_uuid"], item["role"], item["content"],
                         _vec(embedding), item["created_at"], item["sequence_num"]),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO messages
                            (conversation_id, message_uuid, role, content,
                             created_at, sequence_num)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (message_uuid) DO NOTHING
                        """,
                        (conv_id, item["message_uuid"], item["role"], item["content"],
                         item["created_at"], item["sequence_num"]),
                    )

            conn.commit()


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

    name = _session_name(entries, transcript_path, session_id)
    ai_title = _ai_title(entries)

    if not messages and name is None and ai_title is None:
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
            "ai_title":          ai_title,
            "host":              HOST,
            "role":              msg["role"],
            "content":           text,
            "sequence_num":      seq,
            # The real transcript timestamp. In durable mode the drainer may not
            # deliver this for hours, so it must not be re-derived later.
            "created_at":        ts.isoformat(),
        })

    # Archive any images to a local directory (HOME-298). Never raises, and is
    # deliberately attempted in both modes — the archive is local either way.
    try:
        from hook.images import stage_images

        stage_images(
            messages,
            image_sources,
            session_id,
            cwd,
            git_branch,
            text_by_uuid={
                e.get("uuid") or "": _extract_text(e.get("message", {}).get("content", ""))
                for e in messages
            },
        )
    except Exception:
        # Archiving is a nice-to-have; losing the message is not.
        pass

    if not records and name is None and ai_title is None:
        sys.exit(0)

    try:
        if CAPTURE_MODE == "direct":
            _write_to_postgres(records, session_id, cwd, git_branch, name, ai_title)
        else:
            os.makedirs(os.path.dirname(OUTBOX_PATH), exist_ok=True)
            _append_to_outbox(records, session_id, name, ai_title)
    except Exception as exc:
        # Never swallow this. A silent failure here is exactly the bug the
        # durable mode exists to remove — but still exit 0, because blocking
        # Claude from stopping would be a worse outcome than a delayed message.
        _write_fallback(records, str(exc))

    sys.exit(0)


if __name__ == "__main__":
    main()
