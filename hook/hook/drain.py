"""
Drain the local SQLite outbox into Postgres (single-machine edition).

In the distributed edition this job is split three ways: a Rust forwarder ships
outbox rows to an SQS queue, and a consumer on another box drains it, embeds,
and writes to Postgres. That split exists so the write path survives being away
from home. On a single machine there is no network between those pieces, so this
module is all three of them — read the pending rows, embed, write, mark sent.
The distributed edition never runs it: there, the forwarder owns the outbox.

Rows are NOT deleted once written. They are left as tombstones with the content
blanked and `sent_at` set, because record.py re-reads the entire transcript on
every Stop and relies on `INSERT OR IGNORE` against the UNIQUE message_uuid to
suppress re-enqueues. Delete the rows and every session would be re-ingested
from scratch.

Two drains can be started at once — the hook starts one after every capture
(CLAUDE_CHATS_DRAIN_AFTER_CAPTURE) and launchd or systemd starts another on a
timer — so only one is allowed to work at a time. The rest wait on a lock file
beside the outbox, and by the time they get it there is usually nothing left.
The writes are idempotent as well (an upsert on message_uuid), so even two
drains that both got through would rewrite the same rows, not duplicate them.

    drain-outbox                          # drain now
    drain-outbox --env-file config.env    # read settings from a file first

Exits non-zero if anything was left pending, so a scheduler can see the failure.
"""

import argparse
import os
import sqlite3
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone

DEFAULT_DB_URL = "postgresql://claude:claude@localhost:5433/claude_chats"
OUTBOX_PATH = os.environ.get(
    "CLAUDE_CHATS_OUTBOX", os.path.expanduser("~/.claude-chats/outbox.db")
)
# How many messages to take in one pass. Bounded so a months-old backlog drains
# in visible increments rather than one opaque hour-long transaction.
BATCH_SIZE = int(os.environ.get("CLAUDE_CHATS_DRAIN_BATCH", "500"))
# How long a drain waits for another one to finish before leaving it to that
# one. The drain holding the lock keeps going until the outbox is empty, rows
# appended while it runs included, so a waiter only matters in the moment
# between its last look and its exit.
# After this many failed attempts a row is called out on every run. It is
# still retried (behind everything else); a human should look at last_error.
STUCK_AFTER = 10
LOCK_TIMEOUT = float(os.environ.get("CLAUDE_CHATS_DRAIN_LOCK_TIMEOUT", "60"))


def _db_url() -> str:
    # Read at call time, not import time: --env-file may set it after import.
    return os.environ.get("CLAUDE_CHATS_DB_URL", DEFAULT_DB_URL)


def load_env_file(path: str) -> None:
    """Apply ``KEY=VALUE`` lines from install.sh's config.env to os.environ.

    Anything already in the environment wins, so a value given on the command
    line still overrides the remembered one. Not a shell parser: install.sh
    writes plain unquoted lines, and that is all this reads.
    """
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value)


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The next batch, rows that have failed least first.

    Not plain outbox order: a run of rows that fail every time (a poisoned
    conversation, content Postgres rejects) would then fill every batch from
    the front and starve everything captured after it, for ever. Ordered by
    attempts, each failure sends a row behind everything that has not failed
    as often, so new messages keep flowing and the failures are still retried.
    Within a session, sequence_num travels with each row, so the order rows
    are written in does not matter.
    """
    cur = conn.execute(
        """
        SELECT id, message_uuid, session_id, project_path, git_branch,
               conversation_name, ai_title, host, role, author, tool_name, model,
               content, sequence_num, created_at
        FROM outbox
        WHERE sent_at IS NULL
        ORDER BY attempts, id
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


def _mark_sent(conn: sqlite3.Connection, rows: list) -> int:
    """Tombstone rows exactly as they were written; returns how many.

    Guarded on what was written: the hook can change a pending row between
    the Postgres commit and this update — a late /rename refreshes the titles
    of every pending row in its session — and tombstoning the changed row
    would drop that change for good. A row that no longer matches is left
    pending, and the next pass delivers it again.
    """
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        # Content is blanked, not deleted: the row still has to exist to keep
        # suppressing re-inserts, but the text is already safe in Postgres and
        # there is no reason to keep two copies of every conversation on disk.
        before = conn.total_changes
        conn.executemany(
            "UPDATE outbox SET sent_at = ?, content = '', last_error = NULL "
            "WHERE id = ? AND sent_at IS NULL AND content = ? "
            "AND conversation_name IS ? AND ai_title IS ?",
            [(now, r["id"], r["content"], r["conversation_name"], r["ai_title"])
             for r in rows],
        )
        return conn.total_changes - before


def _mark_failed(conn: sqlite3.Connection, ids: list[int], error: str) -> None:
    with conn:
        conn.executemany(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            [(error[:500], i) for i in ids],
        )


# An upsert rather than DO NOTHING, as Cal's consumer does: record.py re-queues
# a delivered row when a late /rename has nothing else to ride on, and the row
# has to land again. Each column only ever moves towards better information:
#   * author never goes back to 'unknown' (an outbox from before HOME-311);
#   * tool_name and model are never blanked by a row that lacks them;
#   * the vector is kept while the text is unchanged, and dropped when the text
#     changed and could not be re-embedded, rather than left describing old text.
_UPSERT_MESSAGE = """
    INSERT INTO messages
        (conversation_id, message_uuid, role, author, tool_name, model,
         content, embedding, created_at, sequence_num)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
    ON CONFLICT (message_uuid) DO UPDATE SET
        role      = EXCLUDED.role,
        author    = CASE WHEN EXCLUDED.author = 'unknown'
                         THEN messages.author ELSE EXCLUDED.author END,
        tool_name = COALESCE(EXCLUDED.tool_name, messages.tool_name),
        model     = COALESCE(EXCLUDED.model, messages.model),
        embedding = CASE WHEN EXCLUDED.content = messages.content
                         THEN COALESCE(EXCLUDED.embedding, messages.embedding)
                         ELSE EXCLUDED.embedding END,
        content   = EXCLUDED.content
"""


def _write_session(cur, rows: list[sqlite3.Row], get_embedding) -> None:
    """Upsert one conversation and its messages. Caller owns the txn."""
    first = rows[0]
    # A rename can arrive in a later batch than the messages it applies to, so
    # take the last non-null name in this batch rather than the first.
    name = next(
        (r["conversation_name"] for r in reversed(rows) if r["conversation_name"]),
        None,
    )
    ai_title = next((r["ai_title"] for r in reversed(rows) if r["ai_title"]), None)
    # COALESCE on both, so a batch without a title never blanks a stored one.
    # started_at is not in the SET: the first message ever seen keeps it.
    cur.execute(
        """
        INSERT INTO conversations
            (session_id, project_path, git_branch, host, name, ai_title, started_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE SET
            name     = COALESCE(EXCLUDED.name, conversations.name),
            ai_title = COALESCE(EXCLUDED.ai_title, conversations.ai_title),
            host     = COALESCE(conversations.host, EXCLUDED.host)
        RETURNING id
        """,
        (first["session_id"], first["project_path"], first["git_branch"],
         first["host"], name, ai_title, first["created_at"]),
    )
    conv_id = cur.fetchone()[0]

    live = [r for r in rows if r["content"]]
    if not live:
        return

    # Skip the embedding call for a row Postgres already has with the same text
    # and a vector — a re-queued row, typically. The upsert keeps that vector.
    cur.execute(
        "SELECT message_uuid, content FROM messages "
        "WHERE message_uuid = ANY(%s) AND embedding IS NOT NULL",
        ([r["message_uuid"] for r in live],),
    )
    embedded = {uuid: content for uuid, content in cur.fetchall()}

    for row in live:
        embedding = None
        if embedded.get(row["message_uuid"]) != row["content"]:
            try:
                embedding = get_embedding(row["content"])
            except Exception:
                # A provider that is down costs the vector, not the message —
                # `install.sh --backfill` fills these in later. Failing the whole
                # drain over an embedding would strand the text as well.
                embedding = None

        cur.execute(
            _UPSERT_MESSAGE,
            (conv_id, row["message_uuid"], row["role"], row["author"],
             row["tool_name"], row["model"], row["content"],
             _vec(embedding) if embedding else None,
             row["created_at"], row["sequence_num"]),
        )


def _acquire_lock(path: str, timeout: float):
    """An exclusive flock on ``path``, or None if another drain kept it."""
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.2)


def _release_lock(fd) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _drain_pass(conn: sqlite3.Connection, pg, rows, get_embedding) -> tuple[int, int, int]:
    """(tombstoned, failed, changed-under-us) message counts for one batch."""
    written = failed = changed = 0
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
            print(f"claude-chats: session {session_id} failed ({exc})", file=sys.stderr)
            continue
        # Only after the Postgres commit — a tombstone written before the data
        # landed would lose the message for good.
        # Counted by what was tombstoned: a row the hook changed meanwhile
        # stays pending and is counted when the next pass delivers it.
        marked = _mark_sent(conn, session_rows)
        written += marked
        changed += len(ids) - marked
    return written, failed, changed


def _anything_pending() -> bool:
    """A cheap look before taking the lock or importing psycopg and the
    embedding providers: the hook starts a drain after every capture, and most
    of them find the work already done."""
    try:
        conn = sqlite3.connect(OUTBOX_PATH, timeout=5)
        try:
            return conn.execute(
                "SELECT 1 FROM outbox WHERE sent_at IS NULL LIMIT 1"
            ).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return True  # no table yet, or locked: let the full path decide


def drain() -> tuple[int, int]:
    """Returns (written, failed) message counts."""
    if not os.path.exists(OUTBOX_PATH) or not _anything_pending():
        return 0, 0

    lock = _acquire_lock(OUTBOX_PATH + ".drain.lock", LOCK_TIMEOUT)
    if lock is None:
        # The drain holding the lock is still working and will take anything
        # this one would have; the timer is the backstop if it dies.
        print("claude-chats: another drain is running; leaving it to that one",
              file=sys.stderr)
        return 0, 0
    try:
        return _drain_locked()
    finally:
        _release_lock(lock)


def _drain_locked() -> tuple[int, int]:
    import psycopg
    from hook.embed import get_embedding
    from hook.record import SCHEMA, _migrate

    conn = sqlite3.connect(OUTBOX_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # The drain can open an outbox before the hook has run since an upgrade
        # (or at all), and _pending selects every column.
        conn.executescript(SCHEMA)
        with conn:
            _migrate(conn)
        rows = _pending(conn)
        if not rows:
            return 0, 0

        try:
            pg = psycopg.connect(_db_url(), autocommit=False)
        except Exception as exc:
            # Postgres being down is the normal case this design exists for, so
            # it is a recorded delay, not a crash. Nothing is lost.
            _mark_failed(conn, [r["id"] for r in rows], str(exc))
            print(f"claude-chats: database unavailable ({exc}); "
                  f"{len(rows)} message(s) left pending", file=sys.stderr)
            return 0, len(rows)

        written = failed = 0
        with pg:
            # Keep going until the outbox is empty, so rows the hook appends
            # while this runs are taken too — that is what lets a drain that
            # found the lock taken simply leave.
            while rows:
                w, f, c = _drain_pass(conn, pg, rows, get_embedding)
                written += w
                failed += f
                # Failed rows sort behind the rest, so carry on while anything
                # is getting through (or was changed by the hook mid-write, and
                # needs writing again); stop once a pass delivers nothing.
                if not w and not c:
                    break
                rows = _pending(conn)
        stuck = conn.execute(
            "SELECT count(*) FROM outbox WHERE sent_at IS NULL AND attempts >= ?",
            (STUCK_AFTER,),
        ).fetchone()[0]
        if stuck:
            print(f"claude-chats: {stuck} message(s) have failed {STUCK_AFTER}+ times "
                  "and are still pending; see last_error in the outbox", file=sys.stderr)
        return written, failed
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Drain the claude-chats outbox into Postgres.")
    ap.add_argument("--env-file", help="read KEY=VALUE settings from this file first")
    args = ap.parse_args()
    if args.env_file:
        try:
            load_env_file(args.env_file)
        except OSError as exc:
            print(f"claude-chats: cannot read {args.env_file} ({exc})", file=sys.stderr)
            sys.exit(1)

    written, failed = drain()
    if written or failed:
        print(f"claude-chats: drained {written} message(s), {failed} left pending")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
