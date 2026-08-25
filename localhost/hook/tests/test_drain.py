"""The outbox drainer (durable capture mode).

Two properties carry the whole design and both fail silently if broken:

1. **A row is only tombstoned after Postgres has committed it.** Mark it sent
   first and a failed write loses the message for good — which is precisely the
   bug durable mode exists to prevent.

2. **Tombstones are kept, never deleted.** record.py re-reads the entire
   transcript on every Stop and relies on INSERT OR IGNORE against the UNIQUE
   message_uuid to suppress re-enqueues. Delete the rows and every session is
   re-ingested from scratch, for ever.
"""

import os
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook import drain as dr  # noqa: E402
from hook.record import SCHEMA  # noqa: E402

FIELDS = (
    "message_uuid", "session_id", "project_path", "git_branch",
    "conversation_name", "host", "role", "content", "sequence_num", "created_at",
)


def _row(uuid, session="sess-1", seq=0, name=None, content="hello"):
    return {
        "message_uuid": uuid,
        "session_id": session,
        "project_path": "/proj",
        "git_branch": "main",
        "conversation_name": name,
        "host": "test-host",
        "role": "user",
        "content": content,
        "sequence_num": seq,
        "created_at": "2026-08-25T12:00:00+00:00",
    }


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    path = tmp_path / "outbox.db"
    monkeypatch.setattr(dr, "OUTBOX_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.close()
    return str(path)


def _insert(path, rows):
    conn = sqlite3.connect(path)
    with conn:
        conn.executemany(
            f"INSERT OR IGNORE INTO outbox ({', '.join(FIELDS)}) "
            f"VALUES ({', '.join(':' + f for f in FIELDS)})",
            rows,
        )
    conn.close()


def _fetch(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


class _FakeRow(dict):
    """sqlite3.Row cannot be constructed from Python, and the drainer only ever
    subscripts by name — so a dict is a faithful enough stand-in."""


class FakeCursor:
    """Records every statement so the tests can assert on the SQL that ran."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return ("conv-id-1",)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestGrouping:
    def test_preserves_outbox_order_within_a_session(self, outbox):
        _insert(outbox, [_row("c", seq=2), _row("a", seq=0), _row("b", seq=1)])
        conn = sqlite3.connect(outbox)
        conn.row_factory = sqlite3.Row
        grouped = dr._group_by_session(dr._pending(conn))
        conn.close()
        assert [r["message_uuid"] for r in grouped["sess-1"]] == ["c", "a", "b"]

    def test_sessions_are_kept_apart(self, outbox):
        _insert(outbox, [_row("a", session="s1"), _row("b", session="s2")])
        conn = sqlite3.connect(outbox)
        conn.row_factory = sqlite3.Row
        grouped = dr._group_by_session(dr._pending(conn))
        conn.close()
        assert set(grouped) == {"s1", "s2"}


class TestTombstones:
    def test_marking_sent_blanks_content_but_keeps_the_row(self, outbox):
        _insert(outbox, [_row("a")])
        conn = sqlite3.connect(outbox)
        dr._mark_sent(conn, [1])
        conn.close()
        rows = _fetch(outbox, "SELECT * FROM outbox")
        assert len(rows) == 1
        assert rows[0]["content"] == ""
        assert rows[0]["sent_at"]

    def test_a_tombstone_suppresses_the_hook_re_enqueueing(self, outbox):
        """The reason tombstones are kept rather than deleted."""
        _insert(outbox, [_row("a")])
        conn = sqlite3.connect(outbox)
        dr._mark_sent(conn, [1])
        conn.close()
        _insert(outbox, [_row("a")])   # what the next Stop hook does
        assert len(_fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")) == 0

    def test_failure_records_the_error_and_leaves_the_row_pending(self, outbox):
        _insert(outbox, [_row("a")])
        conn = sqlite3.connect(outbox)
        dr._mark_failed(conn, [1], "connection refused")
        dr._mark_failed(conn, [1], "connection refused")
        conn.close()
        row = _fetch(outbox, "SELECT * FROM outbox")[0]
        assert row["sent_at"] is None
        assert row["attempts"] == 2
        assert "connection refused" in row["last_error"]


class TestWriteSession:
    def test_upserts_the_conversation_and_inserts_messages(self):
        cur = FakeCursor()
        rows = [_FakeRow(_row("a", seq=0)), _FakeRow(_row("b", seq=1))]
        dr._write_session(cur, rows, lambda text: [0.1, 0.2])
        sql = [c[0] for c in cur.calls]
        assert any(s.startswith("INSERT INTO conversations") for s in sql)
        assert sum(s.startswith("INSERT INTO messages") for s in sql) == 2
        assert all("embedding" in s for s in sql if s.startswith("INSERT INTO messages"))

    def test_a_dead_embedding_provider_still_stores_the_message(self):
        """The text is the irreplaceable part; --backfill can add the vector."""
        cur = FakeCursor()
        def boom(_):
            raise RuntimeError("ollama is not running")
        dr._write_session(cur, [_FakeRow(_row("a"))], boom)
        inserts = [s for s, _ in cur.calls if s.startswith("INSERT INTO messages")]
        assert len(inserts) == 1
        assert "embedding" not in inserts[0]

    def test_the_last_name_in_the_batch_wins(self):
        """A /rename can arrive after the messages it applies to."""
        cur = FakeCursor()
        rows = [_FakeRow(_row("a", seq=0)), _FakeRow(_row("b", seq=1, name="Renamed"))]
        dr._write_session(cur, rows, lambda text: None)
        updates = [p for s, p in cur.calls if s.startswith("UPDATE conversations")]
        assert updates == [("Renamed", "sess-1")]

    def test_no_rename_means_no_update(self):
        cur = FakeCursor()
        dr._write_session(cur, [_FakeRow(_row("a"))], lambda text: None)
        assert not [s for s, _ in cur.calls if s.startswith("UPDATE conversations")]

    def test_tombstoned_rows_in_the_batch_are_skipped(self):
        cur = FakeCursor()
        dr._write_session(cur, [_FakeRow(_row("a", content=""))], lambda text: None)
        assert not [s for s, _ in cur.calls if s.startswith("INSERT INTO messages")]


class TestDrain:
    def test_no_outbox_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dr, "OUTBOX_PATH", str(tmp_path / "absent.db"))
        assert dr.drain() == (0, 0)

    def test_nothing_pending_is_not_an_error(self, outbox):
        assert dr.drain() == (0, 0)

    def test_a_dead_database_leaves_everything_pending(self, outbox, monkeypatch):
        """The normal case durable mode exists for — a delay, not a loss."""
        _insert(outbox, [_row("a"), _row("b")])

        fake = types.ModuleType("psycopg")
        def refuse(*_a, **_kw):
            raise OSError("connection refused")
        fake.connect = refuse
        monkeypatch.setitem(sys.modules, "psycopg", fake)

        assert dr.drain() == (0, 2)
        rows = _fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")
        assert len(rows) == 2
        assert all(r["attempts"] == 1 for r in rows)
        assert all("connection refused" in r["last_error"] for r in rows)

