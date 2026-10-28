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

MAX_CHARS = 3000  # per message; the extractor prompt is ~9k tokens before any conversation


@dataclass
class Session:
    session_id: str
    name: str
    started_at: datetime
    exchanges: list[list[dict]] = field(default_factory=list)
    # When each exchange began (its first message), parallel to exchanges. Sessions can
    # run for weeks, so this, not started_at, is what dates an exchange's facts.
    exchange_times: list[datetime] = field(default_factory=list)


@dataclass
class Exchange:
    """One exchange with where it sits in its session."""
    messages: list[dict]
    started_at: datetime | None = None   # first message's created_at
    first_seq: int | None = None         # sequence_num range of the kept messages
    last_seq: int | None = None
    closed: bool = False                 # True once a later message of Martin's has been seen


def split_exchanges(rows, max_chars=MAX_CHARS):
    """rows: (author, content[, created_at[, sequence_num]]) in sequence order -> [Exchange].

    The one implementation of the exchange rules: build_exchanges (the eval harness) and
    facts.service (production) both go through it, so they cannot disagree about what an
    exchange is. The last exchange is open (closed=False) when the rows end before
    Martin's next message; whether it is finished is the caller's call (idle time).
    """
    out, cur, spoke, prev = [], None, False, None
    for row in rows:
        author, content = row[0], row[1]
        when = row[2] if len(row) > 2 else None
        seq = row[3] if len(row) > 3 else None
        if author not in ("martin", "claude"):
            continue
        if (author, content) == prev:
            continue
        prev = (author, content)
        text = content[:max_chars]
        if author == "martin":
            if cur and spoke:
                cur.closed = True
                out.append(cur)
                cur, spoke = None, False
            if cur is None:
                cur = Exchange([], when, seq, seq)
            cur.messages.append({"role": "user", "content": text})
            cur.last_seq = seq
        elif cur is not None:
            cur.messages.append({"role": "assistant", "content": text})
            cur.last_seq = seq
            spoke = True
    if cur and spoke:
        out.append(cur)
    return out


def build_exchanges(rows, max_chars=MAX_CHARS, with_times=False):
    """rows: (author, content) or (author, content, created_at) in sequence order.

    Returns the exchanges, each a list of {"role": "user"|"assistant", "content": str};
    with_times=True returns (exchanges, times), times being each exchange's first
    message's created_at.
    """
    found = split_exchanges(rows, max_chars)
    exchanges = [e.messages for e in found]
    return (exchanges, [e.started_at for e in found]) if with_times else exchanges


def dsn(arg=None):
    """--db, else $CM_DATABASE_URL, else "" so libpq falls back to PGHOST, PGUSER, PGDATABASE..."""
    return arg or os.environ.get("CM_DATABASE_URL", "")


def load_session(conn, session_id, max_chars=MAX_CHARS) -> Session:
    row = conn.execute(
        "SELECT id, COALESCE(name, ai_title, session_id), started_at FROM conversations WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"session {session_id} is not in conversation-memory")
    conv_id, name, started_at = row
    rows = conn.execute(
        "SELECT author, content, created_at FROM messages WHERE conversation_id = %s ORDER BY sequence_num",
        (conv_id,),
    ).fetchall()
    exchanges, times = build_exchanges(rows, max_chars, with_times=True)
    return Session(session_id, name, started_at, exchanges, times)
