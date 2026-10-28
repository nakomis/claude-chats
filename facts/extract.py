# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""Extract durable facts from one exchange with Claude Haiku (CHATS-23 / CHATS-25).

One call per exchange: the fixed system prompt (cached), the session's recent
messages for resolving references, what this session has already produced, and the
exchange's own date. A reply that is not valid JSON gets one repair attempt.
Anything shaped like a credential is dropped even if the model returns it.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from facts.prompts import KINDS, PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 8000          # thinking at effort low counts towards this too
EARLIER_MESSAGES = 8       # messages before the exchange, for resolving "it" and "that PR"
ALREADY_EXTRACTED = 40     # this session's most recent facts, so they are not repeated

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk-ant-|ghp_|github_pat_|hf_|xox[abp]-)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}"),
]


@dataclass
class Fact:
    text: str
    kind: str | None = None
    subject: str | None = None
    attribute: str | None = None
    temporary: bool = False
    expected_until: str | None = None


@dataclass
class ExtractResult:
    facts: list[Fact]
    calls: list[dict] = field(default_factory=list)
    error: str | None = None
    dropped: list[str] = field(default_factory=list)  # why any returned item was not kept


def looks_secret(text):
    return any(p.search(text) for p in SECRET_PATTERNS)


def _strip_fence(text):
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())


def parse_reply(text):
    """Model reply -> (facts, dropped reasons). Raises ValueError if it is not the JSON asked for."""
    body = _strip_fence(text)
    try:
        data = json.loads(body, strict=False)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", body, re.S)
        if not m:
            raise ValueError("no JSON object in the reply")
        data = json.loads(m.group(0), strict=False)  # may raise: caller repairs
    if not isinstance(data, dict) or not isinstance(data.get("memory"), list):
        raise ValueError('reply has no "memory" list')
    facts, dropped = [], []
    for item in data["memory"]:
        text = (item.get("text") or "").strip() if isinstance(item, dict) else ""
        if not text:
            dropped.append("empty text")
            continue
        if looks_secret(text):
            dropped.append("looks like a credential")
            continue
        kind = item.get("kind")
        until = item.get("expected_until")
        facts.append(Fact(
            text=text,
            kind=kind if kind in KINDS else None,
            subject=(item.get("subject") or None),
            attribute=(item.get("attribute") or "").lower() or None,
            temporary=bool(item.get("temporary")),
            expected_until=until if isinstance(until, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until) else None,
        ))
    return facts, dropped


class Extractor:
    """Calls Haiku through the Anthropic API. client: an anthropic.Anthropic (or compatible)."""

    def __init__(self, client=None, model=DEFAULT_MODEL, effort="low"):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=6)
        self.client, self.model, self.effort = client, model, effort
        self.version = f"{model}+{PROMPT_VERSION}"

    def _call(self, messages, calls):
        t = time.perf_counter()
        r = self.client.messages.create(
            model=self.model, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=messages, output_config={"effort": self.effort})
        text = "".join(b.text for b in r.content if b.type == "text")
        u = r.usage
        calls.append({"uncached_in": u.input_tokens, "cache_read": u.cache_read_input_tokens,
                      "cache_write": u.cache_creation_input_tokens, "output_tokens": u.output_tokens,
                      "seconds": time.perf_counter() - t, "stop": r.stop_reason, "response": text,
                      "messages": messages})
        return text

    def extract(self, new_messages, observation_date, earlier_messages=(), already_extracted=()):
        prompt = build_user_prompt(new_messages, observation_date, earlier_messages, already_extracted)
        messages = [{"role": "user", "content": prompt}]
        calls = []
        try:
            reply = self._call(messages, calls)
            try:
                facts, dropped = parse_reply(reply)
            except (ValueError, json.JSONDecodeError) as e:
                messages = messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"That reply was not the JSON asked for ({e}). "
                                                "Return only the corrected JSON object."}]
                facts, dropped = parse_reply(self._call(messages, calls))
            return ExtractResult(facts, calls, None, dropped)
        except Exception as e:  # noqa: BLE001 - recorded, the caller decides
            return ExtractResult([], calls, repr(e))


class HarnessAdapter:
    """facts_eval's module: interface: add(session, index, exchange) per exchange, in order."""

    def __init__(self, run_dir, extractor=None):
        self.extractor = extractor or Extractor()
        self.name = f"facts:{self.extractor.version}"
        self.by_session = {}

    def add(self, session, index, exchange):
        earlier = [m for ex in session.exchanges[: index - 1] for m in ex][-EARLIER_MESSAGES:]
        times = getattr(session, "exchange_times", None) or []
        when = times[index - 1] if len(times) >= index and times[index - 1] else session.started_at
        date = when.date().isoformat() if isinstance(when, datetime) else str(when or "")[:10]
        seen = self.by_session.setdefault(session.session_id, [])
        res = self.extractor.extract(exchange, date, earlier, seen[-ALREADY_EXTRACTED:])
        seen.extend(f.text for f in res.facts)
        return res


def make_extractor(run_dir):
    return HarnessAdapter(run_dir)
