"""
Claude Code Stop hook.

Reads the session transcript and either embeds messages directly into
PostgreSQL (local mode) or publishes them to an ActiveMQ queue for a
remote worker to process (remote mode).

Mode is controlled by CLAUDE_CHATS_MODE (local|remote, default: local).
Always exits 0 so Claude is never blocked from stopping.
"""

import json
import os
import sys
from datetime import datetime, timezone

DB_URL     = os.environ.get("CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats")
MODE       = os.environ.get("CLAUDE_CHATS_MODE", "local")
QUEUE_URL  = os.environ.get("CLAUDE_CHATS_QUEUE_URL", "stomp://localhost:61613")
QUEUE_NAME = os.environ.get("CLAUDE_CHATS_QUEUE_NAME", "claude-chats-messages")
QUEUE_USER = os.environ.get("CLAUDE_CHATS_QUEUE_USER", "admin")
QUEUE_PASS = os.environ.get("CLAUDE_CHATS_QUEUE_PASS", "admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec_str(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


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


def _parse_stomp_url(url: str) -> tuple[str, int]:
    url = url.removeprefix("stomp://")
    host, _, port = url.rpartition(":")
    return host or "localhost", int(port or 61613)


def _post_to_queue(payload: dict) -> None:
    import stomp  # only needed in remote mode

    host, port = _parse_stomp_url(QUEUE_URL)
    conn = stomp.Connection([(host, port)])
    conn.connect(QUEUE_USER, QUEUE_PASS, wait=True)
    try:
        conn.send(
            destination=f"/queue/{QUEUE_NAME}",
            body=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
    finally:
        conn.disconnect()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:  # noqa: C901
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

    # Build the normalised message list (used by both modes)
    msg_list = []
    for seq, entry in enumerate(messages):
        msg_uuid = entry.get("uuid") or f"{session_id}:{seq}"
        msg      = entry["message"]
        role     = msg["role"]
        text     = _extract_text(msg.get("content", ""))
        if not text:
            continue
        ts_raw = entry.get("timestamp")
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(timezone.utc)
        msg_list.append({
            "message_uuid": msg_uuid,
            "role": role,
            "content": text,
            "created_at": ts.isoformat(),
            "sequence_num": seq,
        })

    # ── Remote mode ──────────────────────────────────────────────────────────
    if MODE == "remote":
        if not msg_list and name is None:
            sys.exit(0)
        try:
            _post_to_queue({
                "session_id":   session_id,
                "project_path": cwd,
                "git_branch":   git_branch,
                "started_at":   datetime.now(timezone.utc).isoformat(),
                "name":         name,
                "messages":     msg_list,
            })
        except Exception:
            pass
        sys.exit(0)

    # ── Local mode ───────────────────────────────────────────────────────────
    from hook.embed import get_embedding
    import psycopg

    try:
        with psycopg.connect(DB_URL, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (session_id, project_path, git_branch)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id) DO NOTHING
                    """,
                    (session_id, cwd, git_branch),
                )
                if name:
                    cur.execute(
                        "UPDATE conversations SET name = %s WHERE session_id = %s",
                        (name, session_id),
                    )
                cur.execute(
                    "SELECT id FROM conversations WHERE session_id = %s",
                    (session_id,),
                )
                conv_id = cur.fetchone()[0]

                cur.execute(
                    "SELECT message_uuid FROM messages WHERE conversation_id = %s",
                    (conv_id,),
                )
                stored = {row[0] for row in cur.fetchall()}

                for item in msg_list:
                    if item["message_uuid"] in stored:
                        continue

                    try:
                        embedding = get_embedding(item["content"])
                    except Exception:
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
                             _vec_str(embedding), item["created_at"], item["sequence_num"]),
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

    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
