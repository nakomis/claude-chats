"""replay-fallback: the fallback file back into the outbox.

The file is the last line of defence, so replaying it must not quietly drop
what the hook captured: author, tool_name, model and the ai title travel with
each line, and a line from before those existed must still replay.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook import replay_fallback as rf  # noqa: E402

BASE = {
    "message_uuid": "m1", "session_id": "s1", "project_path": "/p", "git_branch": "main",
    "conversation_name": None, "host": "h", "role": "assistant", "content": "hi",
    "sequence_num": 0, "created_at": "2026-08-25T12:00:00+00:00",
}


def _replay(tmp_path, monkeypatch, lines):
    fallback = tmp_path / "fallback.ndjson"
    fallback.write_text("".join(json.dumps(line) + "\n" for line in lines))
    monkeypatch.setattr(rf, "OUTBOX_PATH", str(tmp_path / "outbox.db"))
    monkeypatch.setattr(sys, "argv", ["replay-fallback", "--fallback", str(fallback)])
    assert rf.main() == 0
    conn = sqlite3.connect(tmp_path / "outbox.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outbox ORDER BY message_uuid").fetchall()
    conn.close()
    return rows


def test_every_captured_field_survives(tmp_path, monkeypatch):
    rows = _replay(tmp_path, monkeypatch, [{
        **BASE, "ai_title": "A guess", "author": "claude", "tool_name": None,
        "model": "claude-opus-5-5",
    }])
    (row,) = rows
    assert (row["author"], row["model"], row["ai_title"]) == ("claude", "claude-opus-5-5", "A guess")


def test_an_old_line_replays_with_the_outbox_defaults(tmp_path, monkeypatch):
    rows = _replay(tmp_path, monkeypatch, [BASE])
    (row,) = rows
    assert row["author"] == "unknown"
    assert row["tool_name"] is None and row["model"] is None


def test_replaying_into_a_pre_author_outbox_migrates_it(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "outbox.db")
    conn.execute(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, message_uuid TEXT NOT NULL UNIQUE, "
        "session_id TEXT NOT NULL, project_path TEXT NOT NULL DEFAULT '', git_branch TEXT NOT NULL DEFAULT '', "
        "conversation_name TEXT, host TEXT, role TEXT NOT NULL, content TEXT NOT NULL, "
        "sequence_num INTEGER NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, "
        "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
    conn.close()
    (row,) = _replay(tmp_path, monkeypatch, [{**BASE, "author": "tool", "tool_name": "Bash"}])
    assert (row["author"], row["tool_name"]) == ("tool", "Bash")


def test_the_file_is_archived_and_a_second_replay_is_harmless(tmp_path, monkeypatch):
    _replay(tmp_path, monkeypatch, [BASE])
    assert not (tmp_path / "fallback.ndjson").exists()
    assert len(list(tmp_path.glob("fallback.ndjson.*.replayed"))) == 1
    assert len(_replay(tmp_path, monkeypatch, [BASE])) == 1
