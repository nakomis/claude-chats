"""Turn a conversation-memory session into the exchanges an extractor sees.

Only what Martin typed and what Claude said back: tool results (author 'tool') and
unattributed rows ('unknown') are machine output, and on 28 Oct 2026 they made small
models run away and pulled secrets into the prompt. CM also stores some messages twice,
so a message identical to the one before it (same author, same text) is dropped.

An exchange starts at Martin's first message after Claude has spoken, and runs until
his next one: one or more of his messages, then Claude's replies.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_DSN = "postgresql://claude:claude@luke.local:5432/claude_chats"
MAX_CHARS = 3000  # per message; the extractor prompt is ~9k tokens before any conversation


@dataclass
class Session:
    session_id: str
    name: str
    started_at: datetime
    exchanges: list[list[dict]] = field(default_factory=list)


def build_exchanges(rows, max_chars=MAX_CHARS):
    """rows: (author, content) in sequence order -> list of exchanges.

    Each exchange is a list of {"role": "user"|"assistant", "content": str}.
    """
    exchanges, cur, spoke, prev = [], None, False, None
    for author, content in rows:
        if author not in ("martin", "claude"):
            continue
        if (author, content) == prev:
            continue
        prev = (author, content)
        text = content[:max_chars]
        if author == "martin":
            if cur and spoke:
                exchanges.append(cur)
                cur, spoke = None, False
            cur = (cur or []) + [{"role": "user", "content": text}]
        elif cur is not None:
            cur.append({"role": "assistant", "content": text})
            spoke = True
    if cur and spoke:
        exchanges.append(cur)
    return exchanges


def dsn(arg=None):
    return arg or os.environ.get("CM_DATABASE_URL", DEFAULT_DSN)


def load_session(conn, session_id, max_chars=MAX_CHARS) -> Session:
    row = conn.execute(
        "SELECT id, COALESCE(name, ai_title, session_id), started_at FROM conversations WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"session {session_id} is not in conversation-memory")
    conv_id, name, started_at = row
    rows = conn.execute(
        "SELECT author, content FROM messages WHERE conversation_id = %s ORDER BY sequence_num",
        (conv_id,),
    ).fetchall()
    return Session(session_id, name, started_at, build_exchanges(rows, max_chars))
