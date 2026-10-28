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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from facts.prompts import KINDS, PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 8000          # thinking at effort low counts towards this too
EARLIER_MESSAGES = 8       # messages before the exchange, for resolving "it" and "that PR"
ALREADY_EXTRACTED = 40     # this session's most recent facts, so they are not repeated

TZ = ZoneInfo("Europe/London")  # Martin's "yesterday" and "on Friday" are London days

# Best-effort, belt and braces: the prompt is the main defence. Known token shapes,
# plus "password/token/key is|=|: <value>" where the value is not a path or a
# reference to where the secret lives (those facts are wanted).
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk-ant-|sk-proj-|sk-|ghp_|gho_|github_pat_|glpat-|hf_|xox[abprs]-|AIza)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b(?:password|passwd|passphrase|secret|token|api[_\s-]?key)\b\s*(?:is|was|=|:)\s*"
               r"[\"'`]?(?![/~$<\[{(])(?=[^\s\"'`]{6,})(?=[^\s\"'`]*[0-9!@#%^&*_+=-])[^\s\"'`]+"),
]
# Long opaque blobs. No '/' in the class: paths are wanted facts, and far commoner than base64.
HIGH_ENTROPY = re.compile(r"(?<![/\w.-])[A-Za-z0-9+_\-]{32,}(?![/\w.-])")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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
    if any(p.search(text) for p in SECRET_PATTERNS):
        return True
    # A long opaque blob with both letters and digits, and no path or URL around it.
    # UUIDs (session and record ids) are identifiers, not secrets.
    return any(re.search(r"[A-Za-z]", m) and re.search(r"\d", m) and not UUID.fullmatch(m)
               for m in HIGH_ENTROPY.findall(text))


def load_json_object(text):
    """The JSON object in a model reply, tolerating a ``` fence or prose around it.

    Raises ValueError when there is none. Shared with facts_eval so 'unparseable'
    there means exactly what the extractor rejects.
    """
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        return json.loads(body, strict=False)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", body, re.S)
        if not m:
            raise ValueError("no JSON object in the reply") from None
        try:
            return json.loads(m.group(0), strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from None


def _str(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_reply(text):
    """Model reply -> (facts, dropped reasons). Raises ValueError if it is not the JSON asked for.

    Each item is checked on its own: a bad item is dropped with a reason, and never
    costs the good facts beside it.
    """
    data = load_json_object(text)
    if not isinstance(data, dict) or not isinstance(data.get("memory"), list):
        raise ValueError('reply has no "memory" list')
    facts, dropped = [], []
    for item in data["memory"]:
        if not isinstance(item, dict):
            dropped.append("not an object")
            continue
        fact_text = _str(item.get("text"))
        if not fact_text:
            dropped.append("no text")
            continue
        subject, attribute = _str(item.get("subject")), _str(item.get("attribute"))
        if any(looks_secret(s) for s in (fact_text, subject or "", attribute or "")):
            dropped.append("looks like a credential")
            continue
        kind = item.get("kind")
        until = _str(item.get("expected_until"))
        facts.append(Fact(
            text=fact_text,
            kind=kind if kind in KINDS else None,
            subject=subject,
            attribute=attribute.lower() if attribute else None,
            temporary=item.get("temporary") is True,
            expected_until=until if until and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until) else None,
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

    def _call(self, messages, calls, max_tokens=MAX_TOKENS):
        t = time.perf_counter()
        r = self.client.messages.create(
            model=self.model, max_tokens=max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=messages, output_config={"effort": self.effort})
        text = "".join(b.text for b in r.content if b.type == "text")
        u = r.usage
        calls.append({"uncached_in": u.input_tokens, "cache_read": u.cache_read_input_tokens,
                      "cache_write": u.cache_creation_input_tokens, "output_tokens": u.output_tokens,
                      "seconds": time.perf_counter() - t, "stop": r.stop_reason, "response": text,
                      "messages": messages})
        return text, r.stop_reason

    def extract(self, new_messages, observation_date, earlier_messages=(), already_extracted=()):
        """One exchange -> ExtractResult. Never raises: failures are recorded in .error.

        At most two calls. A refusal is recorded, not retried. A reply cut off at the
        token cap, or an empty one, is retried fresh with twice the cap. A complete
        reply that is not valid JSON gets one repair request.
        """
        prompt = build_user_prompt(new_messages, observation_date, earlier_messages, already_extracted)
        messages = [{"role": "user", "content": prompt}]
        calls = []
        try:
            reply, stop = self._call(messages, calls)
            if stop == "refusal":
                return ExtractResult([], calls, "refused")
            if stop == "max_tokens" or not reply.strip():
                reply, stop = self._call(messages, calls, max_tokens=2 * MAX_TOKENS)
                facts, dropped = parse_reply(reply)
                return ExtractResult(facts, calls, None, dropped)
            try:
                facts, dropped = parse_reply(reply)
            except ValueError as e:
                messages = messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"That reply was not the JSON asked for ({e}). "
                                                "Return only the corrected JSON object."}]
                reply, stop = self._call(messages, calls)
                facts, dropped = parse_reply(reply)
            return ExtractResult(facts, calls, None, dropped)
        except Exception as e:  # noqa: BLE001 - recorded, the caller decides
            last = calls[-1]["stop"] if calls else None
            return ExtractResult([], calls, f"{e!r} (last stop: {last})")


def observation_date(when):
    """A message time -> its London calendar date, 'YYYY-MM-DD'. Naive times are taken as UTC."""
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(TZ).date().isoformat()
    return str(when or "")[:10]


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
        date = observation_date(when)
        seen = self.by_session.setdefault(session.session_id, [])
        res = self.extractor.extract(exchange, date, earlier, seen[-ALREADY_EXTRACTED:])
        seen.extend(f.text for f in res.facts)
        return res


def make_extractor(run_dir):
    return HarnessAdapter(run_dir)
