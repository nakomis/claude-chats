"""
Drain the local SQLite outbox into Postgres (durable capture mode only).

In the distributed edition this job is split three ways: a Rust forwarder ships
outbox rows to an SQS queue, and a consumer on another box drains it, embeds,
and writes to Postgres. That split exists so the write path survives being away
from home. On a single machine there is no network between those pieces, so this
module is all three of them — read the pending rows, embed, write, mark sent.

Rows are NOT deleted once written. They are left as tombstones with the content
blanked and `sent_at` set, because record.py re-reads the entire transcript on
every Stop and relies on `INSERT OR IGNORE` against the UNIQUE message_uuid to
suppress re-enqueues. Delete the rows and every session would be re-ingested
from scratch.

Exits non-zero if anything was left pending, so a scheduler can see the failure.
"""

import os
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime, timezone

DB_URL = os.environ.get(
    "CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats"
)
OUTBOX_PATH = os.environ.get(
    "CLAUDE_CHATS_OUTBOX", os.path.expanduser("~/.claude-chats/outbox.db")
)
# How many messages to take in one pass. Bounded so a months-old backlog drains
# in visible increments rather than one opaque hour-long transaction.
BATCH_SIZE = int(os.environ.get("CLAUDE_CHATS_DRAIN_BATCH", "500"))


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(
        """
        SELECT id, message_uuid, session_id, project_path, git_branch,
               conversation_name, host, role, content, sequence_num, created_at
        FROM outbox
        WHERE sent_at IS NULL
        ORDER BY id
        LIMIT ?
        """,
        (BATCH_SIZE,),
    )
    return cur.fetchall()


def _group_by_session(rows: list[sqlite3.Row]) -> "OrderedDict[str, list[sqlite3.Row]]":
    """Group while preserving outbox order, so sequence_num lands in order."""
    grouped: OrderedDict[str, list[sqlite3.Row]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row["session_id"], []).append(row)
    return grouped


def _mark_sent(conn: sqlite3.Connection, ids: list[int]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        # Content is blanked, not deleted: the row still has to exist to keep
        # suppressing re-inserts, but the text is already safe in Postgres and
        # there is no reason to keep two copies of every conversation on disk.
        conn.executemany(
            "UPDATE outbox SET sent_at = ?, content = '', last_error = NULL "
            "WHERE id = ?",
            [(now, i) for i in ids],
        )


def _mark_failed(conn: sqlite3.Connection, ids: list[int], error: str) -> None:
    with conn:
        conn.executemany(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            [(error[:500], i) for i in ids],
        )


def _write_session(cur, rows: list[sqlite3.Row], get_embedding) -> None:
    """Upsert one conversation and insert its messages. Caller owns the txn."""
    first = rows[0]
    cur.execute(
        """
        INSERT INTO conversations (session_id, project_path, git_branch, host, started_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (session_id) DO NOTHING
        """,
        (first["session_id"], first["project_path"], first["git_branch"],
         first["host"], first["created_at"]),
    )

    # A rename can arrive in a later batch than the messages it applies to, so
    # take the last non-null name in this batch rather than the first.
    name = next(
        (r["conversation_name"] for r in reversed(rows) if r["conversation_name"]),
        None,
    )
    if name:
        cur.execute(
            "UPDATE conversations SET name = %s WHERE session_id = %s",
            (name, first["session_id"]),
        )

    cur.execute(
        "SELECT id FROM conversations WHERE session_id = %s", (first["session_id"],)
    )
    conv_id = cur.fetchone()[0]

    for row in rows:
        if not row["content"]:
            continue
        try:
            embedding = get_embedding(row["content"])
        except Exception:
            # A provider that is down costs the vector, not the message —
            # `install.sh --backfill` fills these in later. Failing the whole
            # drain over an embedding would strand the text as well.
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
                (conv_id, row["message_uuid"], row["role"], row["content"],
                 _vec(embedding), row["created_at"], row["sequence_num"]),
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
                (conv_id, row["message_uuid"], row["role"], row["content"],
                 row["created_at"], row["sequence_num"]),
            )


def drain() -> tuple[int, int]:
    """Returns (written, failed) message counts."""
    import psycopg
    from hook.embed import get_embedding

    if not os.path.exists(OUTBOX_PATH):
        return 0, 0

    conn = sqlite3.connect(OUTBOX_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = _pending(conn)
        if not rows:
            return 0, 0

        try:
            pg = psycopg.connect(DB_URL, autocommit=False)
        except Exception as exc:
            # Postgres being down is the normal case this mode exists for, so
            # it is a recorded delay, not a crash. Nothing is lost.
            _mark_failed(conn, [r["id"] for r in rows], str(exc))
            print(f"claude-chats: database unavailable ({exc}); "
                  f"{len(rows)} message(s) left pending", file=sys.stderr)
            return 0, len(rows)

        written = failed = 0
        with pg:
            # Per session, so one poisoned conversation cannot block the rest.
            for session_id, session_rows in _group_by_session(rows).items():
                ids = [r["id"] for r in session_rows]
                try:
                    with pg.cursor() as cur:
                        _write_session(cur, session_rows, get_embedding)
                    pg.commit()
                except Exception as exc:
                    pg.rollback()
                    _mark_failed(conn, ids, str(exc))
                    failed += len(ids)
                    print(f"claude-chats: session {session_id} failed ({exc})",
                          file=sys.stderr)
                    continue
                # Only after the Postgres commit — a tombstone written before
                # the data landed would lose the message for good.
                _mark_sent(conn, ids)
                written += len(ids)

        return written, failed
    finally:
        conn.close()


def main() -> None:
    written, failed = drain()
    if written or failed:
        print(f"claude-chats: drained {written} message(s), {failed} left pending")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
