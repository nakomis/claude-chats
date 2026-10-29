"""The hook starting a drain after capture (single-machine edition).

Only when CLAUDE_CHATS_DRAIN_AFTER_CAPTURE says so — the distributed edition's
forwarder owns its outbox and must never find a local drain competing with it —
and never in a way that can hold up or fail the hook: Claude waits for the hook
on every message, so a drain it waited for would put the database and the
embedding provider straight back on the capture path.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

HOOK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOOK_ROOT)

from hook import drain as dr  # noqa: E402
from hook import record as rec  # noqa: E402


def _transcript(tmp_path, text="hello there"):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({
        "uuid": "msg-1",
        "timestamp": "2026-08-25T12:00:00+00:00",
        "message": {"role": "user", "content": text},
    }) + "\n")
    return path


def _payload(tmp_path):
    return json.dumps({
        "session_id": "sess-1",
        "transcript_path": str(_transcript(tmp_path)),
        "cwd": "/proj",
        "git_branch": "main",
    })


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(rec, "FALLBACK_PATH", str(tmp_path / "fallback.ndjson"))
    monkeypatch.setattr(rec, "CONFIG_PATH", str(tmp_path / "config.env"))
    monkeypatch.setattr(rec, "DRAIN_LOG_PATH", str(tmp_path / "drain.log"))
    from hook import images
    monkeypatch.setattr(images, "STAGING_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(sys, "stdin", _Stdin(_payload(tmp_path)))
    started = []
    monkeypatch.setattr(rec.subprocess, "Popen", lambda *a, **kw: started.append((a, kw)))
    return tmp_path, started


def _run():
    with pytest.raises(SystemExit) as exc:
        rec.main()
    assert exc.value.code == 0


def test_no_drain_unless_asked(hook_env, monkeypatch):
    """The distributed edition's default."""
    _, started = hook_env
    monkeypatch.setattr(rec, "DRAIN_AFTER_CAPTURE", False)
    _run()
    assert started == []


def test_starts_a_detached_drain_after_appending(hook_env, monkeypatch):
    tmp_path, started = hook_env
    (tmp_path / "config.env").write_text("CLAUDE_CHATS_DB_URL=postgresql://x\n")
    monkeypatch.setattr(rec, "DRAIN_AFTER_CAPTURE", True)
    _run()
    ((args, kw),) = started
    assert args[0] == [sys.executable, "-m", "hook.drain",
                       "--env-file", str(tmp_path / "config.env")]
    assert kw["start_new_session"] is True
    # Never the hook's own stdout/stderr: whoever reads those would wait for
    # the drain to close them.
    assert kw["stdout"] not in (None, sys.stdout) and kw["stderr"] not in (None, sys.stderr)
    assert kw["stdin"] is subprocess.DEVNULL
    # The message was already in the outbox before the drain was started.
    conn = sqlite3.connect(tmp_path / "outbox.db")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    conn.close()


def test_no_env_file_argument_when_there_is_no_config(hook_env, monkeypatch):
    _, started = hook_env
    monkeypatch.setattr(rec, "DRAIN_AFTER_CAPTURE", True)
    _run()
    ((args, _),) = started
    assert "--env-file" not in args[0]


def test_a_drain_that_cannot_start_does_not_fail_the_hook(hook_env, monkeypatch, capsys):
    tmp_path, _ = hook_env
    monkeypatch.setattr(rec, "DRAIN_AFTER_CAPTURE", True)
    def boom(*_a, **_kw):
        raise OSError("no such file")
    monkeypatch.setattr(rec.subprocess, "Popen", boom)
    _run()
    assert "could not start a drain" in capsys.readouterr().err
    conn = sqlite3.connect(tmp_path / "outbox.db")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    conn.close()


def test_no_drain_when_the_outbox_write_failed(hook_env, monkeypatch):
    """The message went to the fallback file, which a drain cannot read."""
    _, started = hook_env
    monkeypatch.setattr(rec, "DRAIN_AFTER_CAPTURE", True)
    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(rec, "_append_to_outbox", boom)
    _run()
    assert started == []


def test_the_log_is_rolled_over_rather_than_growing_for_ever(hook_env, monkeypatch):
    tmp_path, _ = hook_env
    monkeypatch.setattr(rec, "DRAIN_AFTER_CAPTURE", True)
    monkeypatch.setattr(rec, "DRAIN_LOG_MAX_BYTES", 10)
    (tmp_path / "drain.log").write_text("x" * 20)
    _run()
    assert (tmp_path / "drain.log.1").read_text() == "x" * 20


def test_the_hook_returns_without_waiting_for_the_drain(tmp_path):
    """End to end, with real processes: the drain is held up on its lock for
    seconds, and the hook — run exactly as Claude Code runs it, with its output
    captured — still finishes at once. Then the drain, released, really runs:
    against a database that is not there, so it records the failure."""
    outbox = tmp_path / "state" / "outbox.db"
    outbox.parent.mkdir()
    (outbox.parent / "config.env").write_text(
        "CLAUDE_CHATS_DB_URL=postgresql://nobody@127.0.0.1:1/none?connect_timeout=2\n")
    sqlite3.connect(outbox).close()
    held = dr._acquire_lock(str(outbox) + ".drain.lock", 1)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CLAUDE_CHATS_OUTBOX": str(outbox),
        "CLAUDE_CHATS_DRAIN_AFTER_CAPTURE": "1",
        "CLAUDE_CHATS_DRAIN_LOCK_TIMEOUT": "30",
    }
    try:
        start = time.monotonic()
        done = subprocess.run(
            [sys.executable, "-m", "hook.record"], input=_payload(tmp_path),
            capture_output=True, text=True, env=env, cwd=HOOK_ROOT, timeout=30,
        )
        elapsed = time.monotonic() - start
        assert done.returncode == 0, done.stderr
        assert elapsed < 10, f"the hook waited {elapsed:.1f}s for its drain"
    finally:
        dr._release_lock(held)

    conn = sqlite3.connect(outbox)
    try:
        deadline = time.monotonic() + 30
        attempts = 0
        while time.monotonic() < deadline:
            attempts = conn.execute("SELECT attempts FROM outbox").fetchone()[0]
            if attempts:
                break
            time.sleep(0.2)
        assert attempts == 1, (outbox.parent / "drain.log").read_text()
    finally:
        conn.close()
