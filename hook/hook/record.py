"""
Claude Code Stop hook.

Reads the session transcript, embeds any new messages, and persists them to
PostgreSQL.  Always exits 0 so Claude is never blocked from stopping.

Embedding provider is configured via CLAUDE_CHATS_PROVIDER — see embed.py.
"""

import json
import os
import sys
from datetime import datetime, timezone

import psycopg

from hook.embed import get_embedding

DB_URL = os.environ.get("CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec_str(embedding: list[float]) -> str:
    """Format a Python list as a PostgreSQL vector literal '[x,y,…]'."""
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

    if not messages:
        sys.exit(0)

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

                for seq, entry in enumerate(messages):
                    msg_uuid = entry.get("uuid") or f"{session_id}:{seq}"

                    if msg_uuid in stored:
                        continue

                    msg     = entry["message"]
                    role    = msg["role"]
                    content = _extract_text(msg.get("content", ""))

                    if not content:
                        continue

                    ts_raw = entry.get("timestamp")
                    ts = (
                        datetime.fromisoformat(ts_raw)
                        if ts_raw
                        else datetime.now(timezone.utc)
                    )

                    try:
                        embedding = get_embedding(content)
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
                            (conv_id, msg_uuid, role, content,
                             _vec_str(embedding), ts, seq),
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
                            (conv_id, msg_uuid, role, content, ts, seq),
                        )

                conn.commit()

    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
