"""Messages sent while Claude is still working (HOME-411).

The bug this guards: a message typed mid-turn is not written to the transcript
as an entry with a ``message``. Claude Code records it as an ``attachment`` of
type ``queued_command`` instead, and the hook, which only read entries carrying
a ``message``, dropped both its text and its images. Nothing failed; they were
simply never there. The fixtures are shaped on real transcript lines, so a
change of format fails here rather than in the database.

Runs under pytest. Standard library only.
"""

import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook import images as im  # noqa: E402
from hook.record import build_records  # noqa: E402

SID = "d3999f16-fa28-4886-b652-23ec8bb408e5"
JPEG = b"\xff\xd8\xff\xe0" + b"a plinth on a flex plate"


def _user(uuid, text, ts="2026-09-30T15:40:00.000Z"):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": SID,
            "message": {"role": "user", "content": text}}


def _assistant(uuid, text, ts="2026-09-30T15:41:00.000Z"):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": SID,
            "message": {"role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": text}]}}


def _queued(uuid, prompt, origin=None, mode="prompt", ts="2026-09-30T15:44:19.693Z"):
    """A mid-turn message, as Claude Code writes it."""
    attachment = {"type": "queued_command", "prompt": prompt,
                  "source_uuid": "47cf8a6f-52ca-418d-af6d-42ef6d022fcd",
                  "commandMode": mode, "timestamp": ts}
    if origin is not None:
        attachment["origin"] = origin
    if mode == "prompt":
        attachment["humanTurn"] = True
    return {"type": "attachment", "uuid": uuid, "timestamp": ts, "sessionId": SID,
            "parentUuid": "67d8fa87-fbdf-4ff1-86b1-5dfe11a2a6ef",
            "cwd": "/Users/martin/repos/nakomis/blog-content", "gitBranch": "main",
            "version": "2.1.285", "attachment": attachment}


HUMAN = {"kind": "human"}


def _image_prompt(text="[Image #1]", data=JPEG):
    return [{"type": "text", "text": text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(data).decode()}}]


def _build(entries):
    records, _messages = build_records(entries, SID, "/p", "main", None, None)
    return records


def _by_uuid(records):
    return {r["message_uuid"]: r for r in records}


# --- text --------------------------------------------------------------------

def test_a_mid_turn_string_is_captured_as_martin():
    recs = _by_uuid(_build([
        _user("u1", "fix the flusher please"),
        _assistant("a1", "Looking now."),
        _queued("q1", "It's possible the image flusher has only ever run on Phi...", HUMAN),
    ]))
    assert "q1" in recs
    assert recs["q1"]["content"] == "It's possible the image flusher has only ever run on Phi..."
    assert recs["q1"]["role"] == "user"
    assert recs["q1"]["author"] == "martin"
    assert recs["q1"]["created_at"].startswith("2026-09-30T15:44:19")


def test_a_text_only_list_prompt_is_still_martin():
    """classify_author would call a list of text blocks 'tool'. Origin decides here."""
    recs = _by_uuid(_build([
        _queued("q1", [{"type": "text", "text": "and another thing"}], HUMAN),
    ]))
    assert recs["q1"]["author"] == "martin"
    assert recs["q1"]["content"] == "and another thing"


def test_older_queued_commands_without_human_turn_are_captured():
    """Two real transcripts carry origin human but no humanTurn flag."""
    entry = _queued("q1", "Is any of it worth negotiating?", HUMAN)
    del entry["attachment"]["humanTurn"]
    assert "q1" in _by_uuid(_build([entry]))


# --- what must stay out ------------------------------------------------------

def test_task_notifications_are_not_captured():
    notice = "<task-notification>\n<task-id>byt3r4cyd</task-id>\n</task-notification>"
    recs = _by_uuid(_build([
        _queued("t1", notice, origin=None, mode="task-notification"),
        _queued("t2", notice, origin={"kind": "task-notification", "producer": "session-task"},
                mode="task-notification"),
    ]))
    assert recs == {}


def test_other_attachments_are_ignored():
    entry = {"type": "attachment", "uuid": "x1", "timestamp": "2026-09-30T15:00:00Z",
             "attachment": {"type": "total_tokens_reminder", "prompt": "not a person"}}
    assert _build([entry]) == []


# --- ordering ----------------------------------------------------------------

def test_existing_sequence_numbers_do_not_move():
    """Rows already in Postgres keep the numbers they were sent with.

    Renumbering everything after a mid-turn message would leave stored rows and
    new ones disagreeing, because the worker never updates a row it has.
    """
    ordinary = [_user("u1", "one"), _assistant("a1", "two"), _user("u2", "three"),
                _assistant("a2", "four")]
    before = {r["message_uuid"]: r["sequence_num"] for r in _build(ordinary)}

    with_queued = ordinary[:2] + [_queued("q1", "mid-turn", HUMAN)] + ordinary[2:]
    after = _by_uuid(_build(with_queued))

    assert {u: after[u]["sequence_num"] for u in before} == before
    # Shares the number of the message before it; created_at breaks the tie.
    assert after["q1"]["sequence_num"] == before["a1"]


def test_a_mid_turn_message_before_any_other_takes_zero():
    recs = _by_uuid(_build([_queued("q1", "first", HUMAN), _user("u1", "second")]))
    assert recs["q1"]["sequence_num"] == 0
    assert recs["u1"]["sequence_num"] == 0


# --- images ------------------------------------------------------------------

def test_a_mid_turn_image_gets_a_marker():
    recs = _by_uuid(_build([_queued("q1", _image_prompt(), HUMAN)]))
    digest = hashlib.sha256(JPEG).hexdigest()[:8]
    assert "[Image #1]" in recs["q1"]["content"]
    assert f"sha256:{digest}" in recs["q1"]["content"]
    assert recs["q1"]["author"] == "martin"


def test_a_mid_turn_image_is_staged_and_marked_mid_turn(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    monkeypatch.setattr(im, "STAGING_DIR", str(staging))
    monkeypatch.setattr(im, "FLUSHED_INDEX", str(staging / ".flushed"))

    _records, messages = build_records(
        [_user("u1", "hello"), _queued("q1", _image_prompt(), HUMAN)],
        SID, "/p", "main", None, None,
    )
    assert im.stage_images(messages, {}, SID, "/p", "main") == 1

    sidecars = list((staging / SID).glob("*.json"))
    assert len(sidecars) == 1
    doc = json.loads(sidecars[0].read_text())
    assert doc["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert doc["mid_turn"] is True
    assert doc["message_uuid"] == "q1"
    assert doc["origin"] == HUMAN


def test_ordinary_images_are_not_marked_mid_turn(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    monkeypatch.setattr(im, "STAGING_DIR", str(staging))
    monkeypatch.setattr(im, "FLUSHED_INDEX", str(staging / ".flushed"))
    entry = {"type": "user", "uuid": "u1", "timestamp": "2026-09-30T15:40:00Z",
             "message": {"role": "user", "content": _image_prompt()}}
    _records, messages = build_records([entry], SID, "/p", "main", None, None)
    im.stage_images(messages, {}, SID, "/p", "main")
    doc = json.loads(next((staging / SID).glob("*.json")).read_text())
    assert "mid_turn" not in doc


# --- idempotence -------------------------------------------------------------

def test_the_uuid_is_the_transcript_uuid():
    """INSERT OR IGNORE on message_uuid is what stops every Stop re-adding it."""
    entries = [_user("u1", "hi"), _queued("q1", "mid-turn", HUMAN)]
    assert [r["message_uuid"] for r in _build(entries)] == [
        r["message_uuid"] for r in _build(entries)
    ] == ["u1", "q1"]


# --- robustness --------------------------------------------------------------

def test_a_bad_timestamp_cannot_cost_the_session():
    """The hook parses every record's timestamp outside a try. One odd value
    from a new source must not raise and lose the whole transcript."""
    for bad in (1727710000, "yesterday", None):
        entry = _queued("q1", "mid-turn", HUMAN)
        entry["attachment"]["timestamp"] = bad
        entry["timestamp"] = bad
        recs = _by_uuid(_build([_user("u1", "hi"), entry]))
        assert recs["q1"]["content"] == "mid-turn"
        assert recs["q1"]["created_at"]


def test_a_bad_attachment_timestamp_falls_back_to_the_entry():
    entry = _queued("q1", "mid-turn", HUMAN)
    entry["attachment"]["timestamp"] = "not a time"
    entry["timestamp"] = "2026-09-30T15:44:19.693Z"
    assert _by_uuid(_build([entry]))["q1"]["created_at"].startswith("2026-09-30T15:44:19")


def test_a_mid_turn_message_without_a_uuid_is_not_captured():
    """Its fallback id would be session:seq, and it shares its predecessor's
    seq, so it would collide and be silently ignored. Leave it out openly."""
    entry = _queued("q1", "orphan", HUMAN)
    del entry["uuid"]
    recs = _build([_user("u1", "hi"), entry])
    assert [r["message_uuid"] for r in recs] == ["u1"]
