# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""Unit tests for facts/: parsing, the secret filter, the repair retry, prompt shape. No network."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "facts_eval"))

from facts import extract, prompts  # noqa: E402
import export  # noqa: E402


class FakeClient:
    """Stands in for anthropic.Anthropic: returns the queued replies in order and records requests."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        usage = SimpleNamespace(input_tokens=10, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, output_tokens=5)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.replies.pop(0))],
                               usage=usage, stop_reason="end_turn")


def reply(*facts):
    return json.dumps({"memory": [{"id": str(i), **f} for i, f in enumerate(facts)]})


def test_parse_reply_keeps_fields_and_normalises():
    facts, dropped = extract.parse_reply(reply(
        {"text": "Mace has 32 GB of RAM.", "kind": "config", "subject": "Mace", "attribute": "RAM"},
        {"text": "Backhaul is down.", "kind": "situation", "subject": "home network", "attribute": "backhaul",
         "temporary": True, "expected_until": "2026-10-30"},
    ))
    assert dropped == []
    assert facts[0] == extract.Fact("Mace has 32 GB of RAM.", "config", "Mace", "ram")
    assert facts[1].temporary is True and facts[1].expected_until == "2026-10-30"


def test_parse_reply_tolerates_fences_and_prose_and_bad_optional_fields():
    text = "Here you go:\n```json\n" + reply({"text": "x is y", "kind": "opinion", "expected_until": "Friday"}) + "\n```"
    (fact,), _ = extract.parse_reply(text)
    assert fact.kind is None and fact.expected_until is None


@pytest.mark.parametrize("secret", [
    "The admin password: hunter2hunter2 is in the README",
    "Key AKIAABCDEFGHIJKLMNOP belongs to the ci user",  # secret-check:allow (fake fixture)
    "token = sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "-----BEGIN OPENSSH PRIVATE KEY----- is in ~/.ssh/id_ed25519",  # secret-check:allow (fake fixture)
])
def test_credential_shaped_facts_are_dropped(secret):
    facts, dropped = extract.parse_reply(reply({"text": secret, "kind": "config"}))
    assert facts == [] and dropped == ["looks like a credential"]


def test_where_a_secret_lives_is_kept():
    text = "The extractor's key is in SSM at /conversation-memory/prod/facts/anthropic-api-key."
    facts, _ = extract.parse_reply(reply({"text": text, "kind": "config"}))
    assert [f.text for f in facts] == [text]


def test_parse_reply_rejects_non_json():
    with pytest.raises(ValueError):
        extract.parse_reply("Sorry, I can't help with that.")


def test_bad_json_gets_one_repair_attempt():
    client = FakeClient('{"memory": [{"id": "0", "text": "a"', reply({"text": "Rey is at 172.29.0.36.", "kind": "config"}))
    res = extract.Extractor(client).extract([{"role": "user", "content": "hi"}], "2026-09-27")
    assert res.error is None and [f.text for f in res.facts] == ["Rey is at 172.29.0.36."]
    assert len(res.calls) == 2
    assert client.requests[1]["messages"][-1]["content"].startswith("That reply was not the JSON")


def test_two_bad_replies_record_an_error():
    res = extract.Extractor(FakeClient("nope", "still nope")).extract([{"role": "user", "content": "hi"}], "2026-09-27")
    assert res.facts == [] and res.error and len(res.calls) == 2


def test_request_caches_the_system_prompt_and_states_the_date():
    client = FakeClient(reply())
    extract.Extractor(client).extract([{"role": "user", "content": "hi"}], "2026-09-27",
                                      earlier_messages=[{"role": "assistant", "content": "before"}],
                                      already_extracted=["Kappa is Tau's son."])
    req = client.requests[0]
    assert req["system"][0]["text"] == prompts.SYSTEM_PROMPT
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"] == {"effort": "low"}
    user = req["messages"][0]["content"]
    assert "## Observation Date\n2026-09-27" in user and "Kappa is Tau's son." in user and "before" in user


def test_adapter_dates_each_exchange_and_feeds_back_what_it_extracted():
    rows = [("martin", "a", datetime(2026, 9, 27, 15, tzinfo=timezone.utc)),
            ("claude", "b", datetime(2026, 9, 27, 15, 1, tzinfo=timezone.utc)),
            ("martin", "c", datetime(2026, 10, 2, 9, tzinfo=timezone.utc)),
            ("claude", "d", datetime(2026, 10, 2, 9, 1, tzinfo=timezone.utc))]
    exchanges, times = export.build_exchanges(rows, with_times=True)
    session = export.Session("s1", "test", times[0], exchanges, times)
    client = FakeClient(reply({"text": "First fact.", "kind": "event"}), reply())
    adapter = extract.HarnessAdapter(None, extract.Extractor(client))
    adapter.add(session, 1, exchanges[0])
    adapter.add(session, 2, exchanges[1])
    second = client.requests[1]["messages"][0]["content"]
    assert "## Observation Date\n2026-10-02" in second
    assert "First fact." in second  # already extracted
    assert '"content": "a"' in second  # earlier messages


def test_system_prompt_covers_the_decisions():
    p = prompts.SYSTEM_PROMPT
    for must in ["Transient readings", "Advice that was not acted on", "Credentials", "IP addresses",
                 "`identity`", "`config`", "`situation`", "`event`", "expected_until", "Observation Date"]:
        assert must in p, must
