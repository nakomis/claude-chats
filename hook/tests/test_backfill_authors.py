"""The author backfill (HOME-311) agrees with the hook and emits valid COPY data.

The backfill must label an old row exactly as the hook would have labelled it
at capture time, or the column ends up meaning two different things depending
on a row's age. It calls the hook's own build_records, and this checks that
it reads every transcript it is given, subagents and restored backups
included.

Runs under pytest. Standard library only.
"""

import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "..", "scripts", "backfill-authors.py")

spec = importlib.util.spec_from_file_location("backfill_authors", SCRIPT)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)

SID = "d3999f16-fa28-4886-b652-23ec8bb408e5"


def _write(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _transcript():
    return [
        {"type": "user", "uuid": "u-typed",
         "message": {"role": "user", "content": "Can you rebuild the map?"}},
        {"type": "assistant", "uuid": "a-call",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "Rebuilding."},
             {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]}},
        {"type": "user", "uuid": "u-result",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
        {"type": "user", "uuid": "u-reminder",
         "message": {"role": "user",
                     "content": "<system-reminder>be brief</system-reminder>"}},
    ]


def test_classifies_as_the_hook_does(tmp_path):
    _write(str(tmp_path / "proj" / f"{SID}.jsonl"), _transcript())
    found = backfill.authors([str(tmp_path)])
    assert found["u-typed"] == ("martin", None, None)
    assert found["a-call"] == ("claude", None, "claude-opus-5-5")
    assert found["u-result"] == ("tool", "Bash", None)
    assert found["u-reminder"][0] == "tool"


def test_reads_subagents_and_every_projects_dir(tmp_path):
    live, restored = tmp_path / "live", tmp_path / "restored"
    _write(str(live / "proj" / SID / "subagents" / "agent-1.jsonl"), _transcript()[:1])
    _write(str(restored / "proj" / "older.jsonl"), [
        {"type": "user", "uuid": "u-old",
         "message": {"role": "user", "content": "from a backup"}}])
    found = backfill.authors([str(live), str(restored)])
    assert set(found) == {"u-typed", "u-old"}


def test_copy_fields_are_escaped():
    assert backfill._copy_field(None) == r"\N"
    assert backfill._copy_field("a\tb\nc\\d") == r"a\tb\nc\\d"
