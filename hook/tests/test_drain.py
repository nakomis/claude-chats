"""The outbox drainer (single-machine edition).

Three properties carry the whole design and all of them fail silently if broken:

1. **A row is only tombstoned after Postgres has committed it.** Mark it sent
   first and a failed write loses the message for good — which is precisely the
   bug the outbox exists to prevent.

2. **Tombstones are kept, never deleted.** record.py re-reads the entire
   transcript on every Stop and relies on INSERT OR IGNORE against the UNIQUE
   message_uuid to suppress re-enqueues. Delete the rows and every session is
   re-ingested from scratch, for ever.

3. **Concurrent drains take turns.** The hook starts one after every capture
   and a timer starts another; two working the same rows at once would embed
   everything twice and race each other's tombstones.
"""

import os
import sqlite3
import subprocess
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook import drain as dr  # noqa: E402
from hook.record import SCHEMA  # noqa: E402
from support import fake_ollama, needs_db, scratch_database  # noqa: E402

FIELDS = (
    "message_uuid", "session_id", "project_path", "git_branch",
    "conversation_name", "ai_title", "host", "role", "author", "tool_name",
    "model", "content", "sequence_num", "created_at",
)


def _row(uuid, session="sess-1", seq=0, name=None, content="hello", ai_title=None,
         role="user", author="martin", tool_name=None, model=None):
    return {
        "message_uuid": uuid,
        "session_id": session,
        "project_path": "/proj",
        "git_branch": "main",
        "conversation_name": name,
        "ai_title": ai_title,
        "host": "test-host",
        "role": role,
        "author": author,
        "tool_name": tool_name,
        "model": model,
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


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
    conn.close()
    return rows


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

    def __init__(self, embedded=()):
        self.calls = []
        self._embedded = list(embedded)

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return ("conv-id-1",)

    def fetchall(self):
        return self._embedded

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def inserts(self):
        return [p for s, p in self.calls if s.startswith("INSERT INTO messages")]


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
        assert dr._mark_sent(conn, _rows(outbox)) == 1
        conn.close()
        rows = _fetch(outbox, "SELECT * FROM outbox")
        assert len(rows) == 1
        assert rows[0]["content"] == ""
        assert rows[0]["sent_at"]

    def test_a_tombstone_suppresses_the_hook_re_enqueueing(self, outbox):
        """The reason tombstones are kept rather than deleted."""
        _insert(outbox, [_row("a")])
        conn = sqlite3.connect(outbox)
        dr._mark_sent(conn, _rows(outbox))
        conn.close()
        _insert(outbox, [_row("a")])   # what the next Stop hook does
        assert len(_fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")) == 0

    def test_a_row_the_hook_changed_after_the_write_is_not_tombstoned(self, outbox):
        """A late /rename lands between the Postgres commit and the tombstone:
        marking the row sent anyway would lose the new name for good."""
        _insert(outbox, [_row("a")])
        as_written = _rows(outbox)
        conn = sqlite3.connect(outbox)
        with conn:
            conn.execute("UPDATE outbox SET conversation_name = 'Late name'")
        assert dr._mark_sent(conn, as_written) == 0
        conn.close()
        (row,) = _rows(outbox)
        assert row["sent_at"] is None and row["content"] == "hello"

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
    def test_upserts_the_conversation_and_its_messages(self):
        cur = FakeCursor()
        rows = [_FakeRow(_row("a", seq=0)), _FakeRow(_row("b", seq=1))]
        dr._write_session(cur, rows, lambda text: [0.1, 0.2])
        sql = [c[0] for c in cur.calls]
        assert any(s.startswith("INSERT INTO conversations") for s in sql)
        assert len(cur.inserts()) == 2
        assert all(p[7] == "[0.1,0.2]" for p in cur.inserts())

    def test_author_tool_name_and_model_reach_postgres(self):
        """Captured by the hook since HOME-311; a drain that dropped them would
        leave every single-machine row 'unknown'."""
        cur = FakeCursor()
        rows = [
            _FakeRow(_row("a", seq=0, role="assistant", author="claude", model="claude-opus-5-5")),
            _FakeRow(_row("b", seq=1, author="tool", tool_name="Bash")),
        ]
        dr._write_session(cur, rows, lambda text: None)
        (a, b) = cur.inserts()
        assert (a[2], a[3], a[4], a[5]) == ("assistant", "claude", None, "claude-opus-5-5")
        assert (b[2], b[3], b[4], b[5]) == ("user", "tool", "Bash", None)

    def test_a_dead_embedding_provider_still_stores_the_message(self):
        """The text is the irreplaceable part; --backfill can add the vector."""
        cur = FakeCursor()
        def boom(_):
            raise RuntimeError("ollama is not running")
        dr._write_session(cur, [_FakeRow(_row("a"))], boom)
        (params,) = cur.inserts()
        assert params[6] == "hello"
        assert params[7] is None

    def test_a_row_postgres_already_has_is_not_re_embedded(self):
        """A re-queued row (a late /rename) must not cost an embedding call."""
        cur = FakeCursor(embedded=[("a", "hello")])
        calls = []
        dr._write_session(cur, [_FakeRow(_row("a"))], lambda t: calls.append(t) or [1.0])
        assert calls == []
        assert len(cur.inserts()) == 1

    def test_changed_text_is_re_embedded(self):
        cur = FakeCursor(embedded=[("a", "old text")])
        calls = []
        dr._write_session(cur, [_FakeRow(_row("a"))], lambda t: calls.append(t) or [1.0])
        assert calls == ["hello"]

    def test_the_last_name_in_the_batch_wins(self):
        """A /rename can arrive after the messages it applies to."""
        cur = FakeCursor()
        rows = [_FakeRow(_row("a", seq=0)), _FakeRow(_row("b", seq=1, name="Renamed"))]
        dr._write_session(cur, rows, lambda text: None)
        (conv,) = [p for s, p in cur.calls if s.startswith("INSERT INTO conversations")]
        assert conv[4:6] == ("Renamed", None)

    def test_titles_never_blank_a_stored_one(self):
        cur = FakeCursor()
        rows = [_FakeRow(_row("a", ai_title="Old guess")),
                _FakeRow(_row("b", seq=1, ai_title="Better guess"))]
        dr._write_session(cur, rows, lambda text: None)
        (sql, params), = [(s, p) for s, p in cur.calls if s.startswith("INSERT INTO conversations")]
        assert "COALESCE(EXCLUDED.name, conversations.name)" in sql
        assert "COALESCE(EXCLUDED.ai_title, conversations.ai_title)" in sql
        assert params[4:6] == (None, "Better guess")

    def test_tombstoned_rows_in_the_batch_are_skipped(self):
        cur = FakeCursor()
        dr._write_session(cur, [_FakeRow(_row("a", content=""))], lambda text: None)
        assert cur.inserts() == []

    def test_the_upsert_only_moves_towards_better_information(self):
        sql = " ".join(dr._UPSERT_MESSAGE.split())
        assert "ON CONFLICT (message_uuid) DO UPDATE" in sql
        assert "WHEN EXCLUDED.author = 'unknown' THEN messages.author" in sql
        assert "COALESCE(EXCLUDED.tool_name, messages.tool_name)" in sql
        assert "COALESCE(EXCLUDED.model, messages.model)" in sql


def _fake_psycopg(monkeypatch, written, on_commit=None, fail_sessions=(), names=None):
    """A psycopg whose connections record every message upsert in ``written``,
    and every conversation name in ``names``. Sessions in ``fail_sessions``
    fail their write; ``on_commit`` runs at each commit."""
    lock = threading.Lock()

    class Cursor(FakeCursor):
        def execute(self, sql, params=None):
            if sql is dr._UPSERT_MESSAGE:
                with lock:
                    written.append(params[1])
            elif "INSERT INTO conversations" in sql:
                if params[0] in fail_sessions:
                    raise RuntimeError("poisoned conversation")
                if names is not None:
                    names.append(params[4])

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            if on_commit:
                on_commit()

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    fake = types.ModuleType("psycopg")
    fake.connect = lambda *_a, **_kw: Conn()
    monkeypatch.setitem(sys.modules, "psycopg", fake)


class TestDrain:
    def test_no_outbox_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dr, "OUTBOX_PATH", str(tmp_path / "absent.db"))
        assert dr.drain() == (0, 0)

    def test_nothing_pending_is_not_an_error(self, outbox):
        assert dr.drain() == (0, 0)

    def test_a_dead_database_leaves_everything_pending(self, outbox, monkeypatch):
        """The normal case the outbox exists for — a delay, not a loss."""
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

    def test_keeps_going_until_the_outbox_is_empty(self, outbox, monkeypatch):
        """A waiting drain gives up and leaves its rows to the running one, so
        the running one has to take rows appended after it started."""
        monkeypatch.setattr(dr, "BATCH_SIZE", 2)
        _insert(outbox, [_row(f"m{i}", seq=i) for i in range(5)])
        written = []
        _fake_psycopg(monkeypatch, written)
        import hook.embed
        monkeypatch.setattr(hook.embed, "get_embedding", lambda t: [1.0])
        assert dr.drain() == (5, 0)
        assert sorted(written) == [f"m{i}" for i in range(5)]

    def test_concurrent_drains_never_deliver_a_row_twice(self, outbox, monkeypatch):
        _insert(outbox, [_row(f"m{i}", seq=i) for i in range(20)])
        written = []
        _fake_psycopg(monkeypatch, written)
        import hook.embed
        def slow(text):
            time.sleep(0.01)  # long enough that the drains overlap
            return [1.0]
        monkeypatch.setattr(hook.embed, "get_embedding", slow)

        results = []
        threads = [threading.Thread(target=lambda: results.append(dr.drain())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(written) == sorted(f"m{i}" for i in range(20))
        assert sum(w for w, _ in results) == 20
        assert not _fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")

    def test_a_drain_that_cannot_get_the_lock_leaves_quietly(self, outbox, monkeypatch):
        _insert(outbox, [_row("a")])
        monkeypatch.setattr(dr, "LOCK_TIMEOUT", 0.3)
        held = dr._acquire_lock(outbox + ".drain.lock", 1)
        try:
            assert dr.drain() == (0, 0)
        finally:
            dr._release_lock(held)
        assert len(_fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")) == 1


class TestPassOrdering:
    def test_failing_rows_at_the_head_do_not_starve_the_rest(self, outbox, monkeypatch):
        """A full batch of rows that always fail used to fill every batch from
        the front, so nothing captured after them was ever delivered."""
        monkeypatch.setattr(dr, "BATCH_SIZE", 2)
        _insert(outbox, [_row("p1", session="poison"), _row("p2", session="poison", seq=1)]
                + [_row(f"f{i}", session="fine", seq=i) for i in range(3)])
        written = []
        _fake_psycopg(monkeypatch, written, fail_sessions={"poison"})
        import hook.embed
        monkeypatch.setattr(hook.embed, "get_embedding", lambda t: [1.0])

        assert dr.drain() == (0, 2)       # the first batch is all poison
        dr.drain()                        # …but now it sorts behind the rest
        assert sorted(written) == ["f0", "f1", "f2"]
        pending = _fetch(outbox, "SELECT message_uuid, attempts FROM outbox WHERE sent_at IS NULL")
        assert sorted(r[0] for r in pending) == ["p1", "p2"]
        assert all(r[1] >= 2 for r in pending)  # still retried, just not first

    def test_a_row_renamed_mid_drain_is_delivered_again_with_its_new_name(self, outbox, monkeypatch):
        _insert(outbox, [_row("a")])
        written, names = [], []

        def rename_once():
            if not names or names[-1] is None:
                c = sqlite3.connect(outbox)
                with c:
                    c.execute("UPDATE outbox SET conversation_name = 'Late name' WHERE sent_at IS NULL")
                c.close()

        _fake_psycopg(monkeypatch, written, on_commit=rename_once, names=names)
        import hook.embed
        monkeypatch.setattr(hook.embed, "get_embedding", lambda t: [1.0])
        assert dr.drain() == (1, 0)
        assert names == [None, "Late name"]
        assert not _fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")

    def test_nothing_pending_returns_before_the_lock(self, outbox, monkeypatch):
        """The hook starts a drain after every capture; most find nothing."""
        def no(*_a, **_kw):
            raise AssertionError("took the lock with nothing to do")
        monkeypatch.setattr(dr, "_acquire_lock", no)
        assert dr.drain() == (0, 0)


class TestEnvFile:
    def test_reads_settings_without_overriding_the_environment(self, tmp_path, monkeypatch):
        f = tmp_path / "config.env"
        f.write_text("# comment\nCLAUDE_CHATS_TEST_A=from-file\nCLAUDE_CHATS_TEST_B=x=y\n\n")
        monkeypatch.delenv("CLAUDE_CHATS_TEST_A", raising=False)
        monkeypatch.setenv("CLAUDE_CHATS_TEST_B", "from-env")
        dr.load_env_file(str(f))
        assert os.environ["CLAUDE_CHATS_TEST_A"] == "from-file"
        assert os.environ["CLAUDE_CHATS_TEST_B"] == "from-env"
        monkeypatch.delenv("CLAUDE_CHATS_TEST_A")


# ---------------------------------------------------------------------------
# Against a real Postgres
# ---------------------------------------------------------------------------

def _run_drain(outbox_path, dsn, ollama_url, env_file=None):
    env = {
        **os.environ,
        "CLAUDE_CHATS_OUTBOX": outbox_path,
        "CLAUDE_CHATS_PROVIDER": "ollama",
        "OLLAMA_BASE_URL": ollama_url,
    }
    args = [sys.executable, "-m", "hook.drain"]
    if env_file:
        args += ["--env-file", env_file]
    else:
        env["CLAUDE_CHATS_DB_URL"] = dsn
    return subprocess.Popen(
        args, env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


@needs_db
class TestAgainstPostgres:
    def test_concurrent_drain_processes_write_each_message_once(self, outbox):
        import psycopg

        _insert(outbox, [
            _row(f"m{i}", seq=i, content=f"message number {i}",
                 role="assistant" if i % 2 else "user",
                 author="claude" if i % 2 else "martin",
                 model="claude-opus-5-5" if i % 2 else None)
            for i in range(30)
        ] + [_row("t", seq=30, content="ls output", author="tool", tool_name="Bash", name="Named")])

        with scratch_database() as dsn, fake_ollama(delay=0.01) as (url, calls):
            procs = [_run_drain(outbox, dsn, url) for _ in range(3)]
            outs = [p.communicate(timeout=120)[0] for p in procs]
            assert [p.returncode for p in procs] == [0, 0, 0], outs
            # One embedding per message: nobody did anyone else's work.
            assert sorted(calls) == sorted([f"message number {i}" for i in range(30)] + ["ls output"])

            with psycopg.connect(dsn) as conn:
                rows = conn.execute(
                    "SELECT message_uuid, author, tool_name, model, embedding IS NOT NULL "
                    "FROM messages ORDER BY sequence_num").fetchall()
                conv = conn.execute("SELECT name, host FROM conversations").fetchall()
        assert len(rows) == 31
        assert all(r[4] for r in rows)
        assert rows[1][1:4] == ("claude", None, "claude-opus-5-5")
        assert rows[30][1:4] == ("tool", "Bash", None)
        assert conv == [("Named", "test-host")]
        assert not _fetch(outbox, "SELECT * FROM outbox WHERE sent_at IS NULL")

    def test_a_requeued_row_lands_again_without_losing_anything(self, outbox, tmp_path):
        """What record.py's late-rename path does: a delivered row comes back
        with its content refilled and a new name."""
        import psycopg

        _insert(outbox, [_row("a", content="first words", author="martin")])
        with scratch_database() as dsn, fake_ollama() as (url, calls):
            env_file = tmp_path / "config.env"
            env_file.write_text(f"CLAUDE_CHATS_DB_URL={dsn}\n")
            assert _run_drain(outbox, dsn, url, env_file=str(env_file)).wait(60) == 0
            conn = sqlite3.connect(outbox)
            with conn:
                conn.execute("UPDATE outbox SET sent_at = NULL, content = 'first words', "
                             "conversation_name = 'Late name', author = 'unknown'")
            conn.close()
            assert _run_drain(outbox, dsn, url, env_file=str(env_file)).wait(60) == 0
            assert calls == ["first words"]  # not embedded a second time
            with psycopg.connect(dsn) as pg:
                msg = pg.execute("SELECT content, author, embedding IS NOT NULL FROM messages").fetchall()
                name = pg.execute("SELECT name FROM conversations").fetchone()[0]
        assert msg == [("first words", "martin", True)]
        assert name == "Late name"
