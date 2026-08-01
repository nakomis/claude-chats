"""Author classification, and the migration that makes room for it (HOME-310).

The bug this prevents: Claude Code delivers tool results as role='user'
messages, so every command output and file dump used to be indistinguishable
from something the human typed. Measured across 134 transcripts, only 14% of
role='user' rows were actually typed by a person (HOME-309).

Runs standalone (``python hook/tests/test_author_classification.py``) or under
pytest. Standard library only.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook.record import (  # noqa: E402
    SCHEMA,
    _migrate,
    _tool_name_for,
    _tool_use_names,
    classify_author,
)


def test_string_content_from_user_is_the_human():
    assert classify_author("user", "Why aren't you using the Taiga MCP?") == "martin"


def test_assistant_is_claude_whatever_the_content_shape():
    assert classify_author("assistant", "Here you go") == "claude"
    assert classify_author("assistant", [{"type": "text", "text": "hi"}]) == "claude"


def test_tool_result_list_is_a_tool():
    content = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
    assert classify_author("user", content) == "tool"


def test_harness_text_blocks_are_not_the_human():
    """Interrupts and system notices arrive as a list of text blocks.

    They are not typed by anyone, so they must not land in the human bucket.
    The list-vs-string distinction is what separates them; a human message is
    always a bare string.
    """
    content = [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
    assert classify_author("user", content) == "tool"


def test_long_technical_prose_from_the_human_stays_human():
    """The failure mode of every text-based approach.

    A pasted diff looks exactly like tool output, because it is tool output --
    the human just chose to paste it. Structure knows the difference; content
    never can. A regex managed 92.6% precision at 21.8% recall here.
    """
    pasted = (
        "services:\n  scrutiny-collector:\n"
        "    image: ghcr.io/analogj/scrutiny:master-collector\n"
        "    cap_add: [SYS_RAWIO]\n"
    )
    assert classify_author("user", pasted) == "martin"


def test_bare_system_reminder_is_not_the_human():
    """Harness injections that arrive as a plain string, not a content block.

    Rare (11 of 1,841 string messages across 134 transcripts) but they would
    otherwise slip into the human bucket, since the structural rule reads any
    string as human.
    """
    msg = ('<system-reminder> The user named this session "substack". '
           'This may indicate the topic. </system-reminder>')
    assert classify_author("user", msg) == "tool"


def test_human_text_alongside_a_reminder_stays_human():
    """The other half of the rule, and why it is a subtraction not a search.

    Measured across the same transcripts, a reminder never arrives appended to
    real text -- but if it ever does, the human's words must win. Testing for
    the *presence* of a reminder would get this backwards.
    """
    msg = ('Can you check the blog pipeline?\n'
           '<system-reminder> Some note. </system-reminder>')
    assert classify_author("user", msg) == "martin"


def test_tool_name_resolves_through_the_tool_use_id():
    entries = [
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash"}]}},
    ]
    names = _tool_use_names(entries)
    result = [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "x"}]
    assert _tool_name_for(result, names) == "Bash"


def test_tool_name_is_none_when_the_call_is_not_in_this_transcript():
    """Truncated or resumed transcripts can hold a result whose call is absent.

    Returning None beats inventing a name -- an absent tool is recoverable
    later, a wrong one is not.
    """
    result = [{"type": "tool_result", "tool_use_id": "missing", "content": "x"}]
    assert _tool_name_for(result, {}) is None


def test_migration_adds_columns_to_an_existing_outbox():
    """The case a plain schema edit would miss.

    CREATE TABLE IF NOT EXISTS does nothing against a database that already
    exists, so without _migrate every existing outbox would fail its next
    INSERT on a missing column.
    """
    old_ddl = """
    CREATE TABLE outbox (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        message_uuid TEXT NOT NULL UNIQUE,
        session_id   TEXT NOT NULL,
        role         TEXT NOT NULL,
        content      TEXT NOT NULL,
        sequence_num INTEGER NOT NULL,
        created_at   TEXT NOT NULL
    );
    """
    with tempfile.TemporaryDirectory() as d:
        conn = sqlite3.connect(os.path.join(d, "outbox.db"))
        conn.executescript(old_ddl)
        conn.execute(
            "INSERT INTO outbox (message_uuid, session_id, role, content,"
            " sequence_num, created_at) VALUES ('u1','s1','user','hello',0,'now')")
        conn.commit()

        _migrate(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
        assert {"author", "tool_name", "model"} <= cols

        # Pre-existing rows are marked unknown, never guessed at: 'unknown' is
        # honest and greppable, whereas defaulting to 'martin' would silently
        # manufacture exactly the bad data HOME-309 is about.
        assert conn.execute("SELECT author FROM outbox").fetchone()[0] == "unknown"
        conn.close()


def test_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        conn = sqlite3.connect(os.path.join(d, "outbox.db"))
        conn.executescript(SCHEMA)
        _migrate(conn)
        _migrate(conn)  # must not raise on the second pass
        cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
        assert {"author", "tool_name", "model"} <= cols
        conn.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
