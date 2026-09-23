"""Session names and generated titles (HOME-391).

The bug this guards: the hook found a /rename by looking for a ``local_command``
system entry wrapping ``<command-name>/rename``. Claude Code stopped writing
that, and every session renamed afterwards reached the database with no name —
silently, for weeks. The fixtures below are shaped on real transcript lines, so
a change of format fails here instead of in the database.

Also covers the path a title takes to the outbox, including the case the old
code lost outright: a rename as the last act of a session, when every message
row has already been sent.

Single-machine edition copy of hook/tests/test_session_titles.py. Runs
standalone (``python localhost/hook/tests/test_session_titles.py``) or under pytest.
Standard library only.
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hook.record as record  # noqa: E402
from hook.record import _ai_title, _session_name  # noqa: E402

SID = "86b16045-5c80-4d00-aaec-dc1751d7a482"


def _user(uuid: str, text: str) -> dict:
    return {
        "parentUuid": None, "isSidechain": False, "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": uuid, "timestamp": "2026-09-19T17:03:53.485Z", "sessionId": SID,
    }


def _assistant(uuid: str, text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": text}]},
        "uuid": uuid, "timestamp": "2026-09-19T17:04:10.000Z", "sessionId": SID,
    }


def _custom(title: str) -> dict:
    return {"type": "custom-title", "customTitle": title, "sessionId": SID}


def _ai(title: str) -> dict:
    return {"type": "ai-title", "aiTitle": title, "sessionId": SID}


def _last_prompt() -> dict:
    return {"type": "last-prompt", "leafUuid": "0efc0547-8de7-4aeb-a9b2-e85038863706",
            "sessionId": SID}


def _legacy_rename(name: str) -> dict:
    return {
        "type": "system", "subtype": "local_command",
        "content": "<command-name>/rename</command-name>\n"
                   "<command-message>rename</command-message>\n"
                   f"<command-args>{name}</command-args>",
    }


def _project_dir(sidecar: str | None = None) -> str:
    """A project directory holding a transcript path, optionally with a sidecar."""
    d = tempfile.mkdtemp()
    if sidecar is not None:
        os.makedirs(os.path.join(d, SID))
        with open(os.path.join(d, SID, "custom-title.json"), "w") as fh:
            fh.write(sidecar)
    return os.path.join(d, f"{SID}.jsonl")


# --- names -------------------------------------------------------------------

def test_inline_custom_title_is_the_name():
    entries = [_user("u1", "hello"), _ai("Adding IMDb links"), _custom("add-imdb-to-nakom.is"),
               _last_prompt(), _assistant("a1", "hi")]
    assert _session_name(entries, _project_dir(), SID) == "add-imdb-to-nakom.is"


def test_the_later_rename_wins():
    entries = [_custom("first"), _user("u1", "hello"), _custom("second"), _custom("second")]
    assert _session_name(entries, _project_dir(), SID) == "second"


def test_sidecar_alone_is_found():
    path = _project_dir(json.dumps({"customTitle": "add-imdb-to-nakom.is"}))
    assert _session_name([_user("u1", "hello")], path, SID) == "add-imdb-to-nakom.is"


def test_sidecar_beats_inline():
    path = _project_dir(json.dumps({"customTitle": "from-sidecar"}))
    assert _session_name([_custom("from-transcript")], path, SID) == "from-sidecar"


def test_malformed_sidecar_falls_through_without_raising():
    path = _project_dir("{not json")
    assert _session_name([_custom("inline")], path, SID) == "inline"
    assert _session_name([], path, SID) is None


def test_legacy_rename_is_still_found():
    entries = [_user("u1", "hello"), _legacy_rename("old-style")]
    assert _session_name(entries, _project_dir(), SID) == "old-style"


def test_ai_title_is_never_a_name():
    entries = [_user("u1", "hello"), _ai("Something Claude made up")]
    assert _session_name(entries, _project_dir(), SID) is None


def test_skill_command_tags_in_a_user_message_are_not_a_rename():
    # Real transcripts carry <command-name> inside user messages for skills.
    entries = [_user("u1", "<command-message>rename</command-message>\n"
                           "<command-name>/rename</command-name>")]
    assert _session_name(entries, _project_dir(), SID) is None


# --- ai titles ---------------------------------------------------------------

def test_latest_ai_title_wins():
    entries = [_ai("First guess"), _user("u1", "hello"), _ai("Better guess")]
    assert _ai_title(entries) == "Better guess"


def test_no_ai_title():
    assert _ai_title([_user("u1", "hello"), _custom("named")]) is None


# --- outbox ------------------------------------------------------------------

def _outbox() -> str:
    path = os.path.join(tempfile.mkdtemp(), "outbox.db")
    record.OUTBOX_PATH = path
    return path


def _rec(uuid: str, seq: int, name=None, ai=None, content="text") -> dict:
    return {
        "message_uuid": uuid, "session_id": SID, "project_path": "/p", "git_branch": "main",
        "conversation_name": name, "ai_title": ai, "host": "phi", "role": "user",
        "author": "martin", "tool_name": None, "model": None, "content": content,
        "sequence_num": seq, "created_at": "2026-09-23T15:00:00+00:00",
    }


def _send_all(path: str) -> None:
    """What the forwarder does to a delivered row: tombstone it."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE outbox SET sent_at = '1', content = '' WHERE sent_at IS NULL")
    conn.close()


def _rows(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT message_uuid, conversation_name, ai_title, content, sent_at "
            "FROM outbox ORDER BY sequence_num"
        ).fetchall()
    finally:
        conn.close()


def test_titles_are_stored_on_new_rows():
    path = _outbox()
    record._append_to_outbox([_rec("u1", 0, "n", "a")], SID, "n", "a")
    assert _rows(path) == [("u1", "n", "a", "text", None)]


def test_late_title_updates_pending_rows_without_blanking():
    path = _outbox()
    record._append_to_outbox([_rec("u1", 0, "n", "a")], SID, "n", "a")
    # A later run that found an ai title but (somehow) no name must not null the name.
    record._append_to_outbox([_rec("u1", 0)], SID, None, "a2")
    assert _rows(path) == [("u1", "n", "a2", "text", None)]


def test_rename_after_everything_was_sent_revives_the_newest_row():
    path = _outbox()
    recs = [_rec("u1", 0, content="first"), _rec("u2", 1, content="second")]
    record._append_to_outbox(recs, SID, None, None)
    _send_all(path)

    # SessionEnd after a last-thing /rename: no new messages, just a name.
    record._append_to_outbox(recs, SID, "late-name", None)

    rows = _rows(path)
    assert rows[0] == ("u1", None, None, "", "1"), "older tombstones stay put"
    # Refilled from the transcript, never resent blank: the consumer's upsert
    # overwrites content, so a blank resend would erase the message in Postgres.
    assert rows[1] == ("u2", "late-name", None, "second", None)


def test_no_revival_when_nothing_changed():
    path = _outbox()
    recs = [_rec("u1", 0, "n", "a")]
    record._append_to_outbox(recs, SID, "n", "a")
    _send_all(path)
    record._append_to_outbox(recs, SID, "n", "a")
    assert _rows(path) == [("u1", "n", "a", "", "1")]


def test_no_revival_when_the_row_is_not_in_the_transcript():
    path = _outbox()
    record._append_to_outbox([_rec("u1", 0)], SID, None, None)
    _send_all(path)
    record._append_to_outbox([], SID, "late-name", None)
    assert _rows(path) == [("u1", None, None, "", "1")]


def test_migration_adds_ai_title_to_an_old_outbox():
    path = _outbox()
    conn = sqlite3.connect(path)
    conn.executescript(record.SCHEMA.replace("    ai_title          TEXT,\n", ""))
    conn.close()
    record._append_to_outbox([_rec("u1", 0, "n", "a")], SID, "n", "a")
    assert _rows(path) == [("u1", "n", "a", "text", None)]


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok: {_name}")
