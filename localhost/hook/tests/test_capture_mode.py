"""Capture-mode dispatch in the hook.

The single-machine edition has two ways to record a message and one script that
does both, so the thing worth pinning is that the right one runs — and that when
it fails, the message lands in the fallback file rather than vanishing. Silent
loss on the capture path is the original bug the whole design is a response to,
and it is invisible until someone goes looking for a conversation that was never
stored.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook import record as rec  # noqa: E402


def _transcript(tmp_path, text="hello there"):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({
        "uuid": "msg-1",
        "timestamp": "2026-08-25T12:00:00+00:00",
        "message": {"role": "user", "content": text},
    }) + "\n")
    return path


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Point the hook at a scratch outbox and feed it one message on stdin."""
    outbox = tmp_path / "outbox.db"
    monkeypatch.setattr(rec, "OUTBOX_PATH", str(outbox))
    monkeypatch.setattr(rec, "FALLBACK_PATH", str(tmp_path / "fallback.ndjson"))
    # Archiving is exercised by test_images.py and only gets in the way here.
    # Keep it pointed at tmp_path regardless: main() calls stage_images, and a
    # test has no business walking the real ~/.claude-chats/images.
    monkeypatch.setattr(rec, "_image_markers", lambda *a, **kw: [])
    from hook import images
    monkeypatch.setattr(images, "STAGING_DIR", str(tmp_path / "images"))

    payload = json.dumps({
        "session_id": "sess-1",
        "transcript_path": str(_transcript(tmp_path)),
        "cwd": "/proj",
        "git_branch": "main",
    })
    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    return tmp_path


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _run():
    with pytest.raises(SystemExit) as exc:
        rec.main()
    assert exc.value.code == 0


class TestDurableMode:
    def test_appends_to_the_outbox_and_does_not_touch_postgres(self, hook_env, monkeypatch):
        monkeypatch.setattr(rec, "CAPTURE_MODE", "durable")
        monkeypatch.setattr(rec, "_write_to_postgres", _explode)
        _run()
        rows = _outbox_rows(hook_env / "outbox.db")
        assert [r["content"] for r in rows] == ["hello there"]

    def test_running_twice_does_not_duplicate(self, hook_env, monkeypatch):
        """The hook re-reads the whole transcript on every Stop."""
        monkeypatch.setattr(rec, "CAPTURE_MODE", "durable")
        _run()
        _run()
        assert len(_outbox_rows(hook_env / "outbox.db")) == 1

    def test_an_unwritable_outbox_falls_back_rather_than_losing_the_message(
        self, hook_env, monkeypatch
    ):
        monkeypatch.setattr(rec, "_append_to_outbox", _explode)
        monkeypatch.setattr(rec, "CAPTURE_MODE", "durable")
        _run()
        assert "hello there" in (hook_env / "fallback.ndjson").read_text()


class TestDirectMode:
    def test_writes_to_postgres_and_not_the_outbox(self, hook_env, monkeypatch):
        seen = {}
        def capture(records, session_id, project_path, git_branch, name):
            seen["records"] = records
            seen["session_id"] = session_id
        monkeypatch.setattr(rec, "CAPTURE_MODE", "direct")
        monkeypatch.setattr(rec, "_write_to_postgres", capture)
        _run()
        assert [r["content"] for r in seen["records"]] == ["hello there"]
        assert seen["session_id"] == "sess-1"
        assert not (hook_env / "outbox.db").exists()

    def test_a_dead_database_falls_back_rather_than_losing_the_message(
        self, hook_env, monkeypatch
    ):
        """Direct mode's whole risk, and the reason it keeps the fallback."""
        monkeypatch.setattr(rec, "CAPTURE_MODE", "direct")
        monkeypatch.setattr(rec, "_write_to_postgres", _explode)
        _run()
        assert "hello there" in (hook_env / "fallback.ndjson").read_text()


def _explode(*_a, **_kw):
    raise RuntimeError("nope")


def _outbox_rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outbox").fetchall()
    conn.close()
    return rows
