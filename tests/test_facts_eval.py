"""Unit tests for the facts eval harness: no database, no network."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "facts_eval"))

import export  # noqa: E402
import extractors  # noqa: E402
import judge  # noqa: E402

EXAMPLE = Path(__file__).resolve().parent.parent / "facts_eval" / "example"


def test_exchanges_skip_tool_output_and_unknown_rows():
    rows = [
        ("martin", "Is Rey charging?"),
        ("tool", "POWER_SUPPLY_ONLINE=0"),
        ("unknown", "{}"),
        ("claude", "No, the charger is not detected."),
    ]
    assert export.build_exchanges(rows) == [[
        {"role": "user", "content": "Is Rey charging?"},
        {"role": "assistant", "content": "No, the charger is not detected."},
    ]]


def test_consecutive_duplicates_are_dropped_but_repeats_later_are_kept():
    rows = [
        ("martin", "FUSE FS*"), ("martin", "FUSE FS*"),
        ("claude", "Checking."), ("claude", "Checking."), ("claude", "Done."),
        ("martin", "Thanks"), ("claude", "Checking."),
    ]
    ex = export.build_exchanges(rows)
    assert [m["content"] for m in ex[0]] == ["FUSE FS*", "Checking.", "Done."]
    assert [m["content"] for m in ex[1]] == ["Thanks", "Checking."]


def test_new_exchange_starts_only_after_claude_has_spoken():
    rows = [("martin", "a"), ("martin", "b"), ("claude", "c"), ("martin", "d"), ("claude", "e")]
    ex = export.build_exchanges(rows)
    assert [[m["content"] for m in e] for e in ex] == [["a", "b", "c"], ["d", "e"]]


def test_leading_claude_messages_and_unanswered_tail_are_dropped():
    rows = [("claude", "hello"), ("martin", "q"), ("claude", "a"), ("martin", "no reply yet")]
    assert [[m["content"] for m in e] for e in export.build_exchanges(rows)] == [["q", "a"]]


def test_messages_are_cut_to_max_chars():
    ex = export.build_exchanges([("martin", "x" * 50), ("claude", "y" * 50)], max_chars=10)
    assert [len(m["content"]) for m in ex[0]] == [10, 10]


@pytest.mark.parametrize("spec,expected", [
    ("mem0:anthropic:claude-haiku-5-5", ("mem0", "anthropic", "claude-haiku-5-5")),
    ("mem0:bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0",
     ("mem0", "bedrock", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")),
    ("mem0:ollama:qwen2.5:14b", ("mem0", "ollama", "qwen2.5:14b")),
    ("module:facts.extract:make_extractor", ("module", "facts.extract", "make_extractor")),
])
def test_parse_spec(spec, expected):
    assert extractors.parse_spec(spec) == expected


@pytest.mark.parametrize("spec", ["mem0:openai:gpt", "mem0:anthropic:", "module:nofactory", "haiku"])
def test_parse_spec_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        extractors.parse_spec(spec)


@pytest.mark.parametrize("text,ok", [
    ('{"memory": []}', True),
    ('```json\n{"memory": ["a"]}\n```', True),
    ('Here you go: {"memory": ["a"]} hope that helps', True),
    ('{"memory": ["a", "b"', False),
    ("", True),
    (None, None),
])
def test_parses_as_json(text, ok):
    assert extractors.parses_as_json(text) is ok


def test_parse_verdict_fills_gaps_as_missing_and_unjudged():
    reply = 'Sure. {"gold": {"R1": "full", "R2": "partial"}, "memories": [{"n": 1, "verdict": "correct"}]}'
    v = judge.parse_verdict(reply, ["R1", "R2", "R3"], 2)
    assert v == {"gold": {"R1": "full", "R2": "partial", "R3": "missing"}, "memories": ["correct", "unjudged"]}


def test_summarise_recall_counts_partial_as_half():
    s = judge.summarise([
        {"gold": {"A": "full", "B": "partial"}, "memories": ["correct", "wrong"]},
        {"gold": {"C": "missing", "D": "full"}, "memories": ["advice"]},
    ])
    assert s["recall"] == pytest.approx(2.5 / 4)
    assert (s["full"], s["partial"], s["missing"], s["facts"]) == (2, 1, 1, 3)
    assert (s["correct"], s["wrong"], s["advice"], s["transient"], s["trivial"]) == (1, 1, 1, 0, 0)


def test_example_reference_matches_example_session():
    ref = json.loads((EXAMPLE / "reference.json").read_text())
    rows = json.loads((EXAMPLE / "session.json").read_text())["messages"]
    session = export.Session("example", "example", None, export.build_exchanges([tuple(r) for r in rows]))
    prompt = judge.build_prompt(session, ref["sessions"][0]["facts"], ["Kappa is Chi and Tau's son."])
    assert "M: " in prompt and "C: " in prompt and "X1:" in prompt
    assert "tool output" not in judge.transcript(session)
