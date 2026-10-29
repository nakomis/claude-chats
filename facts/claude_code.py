# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""Extract facts through the user's own Claude Code login: `claude -p` (CHATS-39).

The single-machine edition has no API key of its own, so each call is a headless
Claude Code run on the user's subscription. Every run is isolated: no tools, no MCP
servers, no settings files, no hooks, no saved session, an empty working directory
(so no CLAUDE.md is found), and ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN removed from
its environment so Claude Code bills the login and not a key that happens to be set.
Never --bare: it ignores the login and only accepts an API key.

The reply is forced into the prompt's output shape with --json-schema; the facts
still go through parse_reply, so the credential filter and field checks apply.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from facts.extract import MAX_TOKENS, SYSTEMIC_STATUS, Extractor
from facts.prompts import KINDS, PROMPT_VERSION, SYSTEM_PROMPT

DEFAULT_TIMEOUT = 120.0  # seconds per call; the child is killed after this
DEFAULT_WORKERS = 3      # claude -p is a whole process: keep sessions in parallel low
# Anything that would make Claude Code bill something other than the login (a key, Bedrock,
# Vertex, Foundry) or send the call somewhere else or to another model. The login itself
# (CLAUDE_CODE_OAUTH_TOKEN, when set instead of a stored login) is kept: it is what pays.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_FOUNDRY_API_KEY")

# Mirrors the OUTPUT FORMAT section of the system prompt.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"memory": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "subject": {"type": "string"},
            "attribute": {"type": "string"},
            "temporary": {"type": "boolean"},
            "expected_until": {"type": "string"},
        },
        "required": ["id", "text", "kind", "subject", "attribute"],
        "additionalProperties": False,
    }}},
    "required": ["memory"],
    "additionalProperties": False,
}

# An error reply that is about the login or the service, not the exchange.
SYSTEMIC_TEXT = re.compile(r"(?i)not logged in|/login|log ?in again|oauth token|token (?:has )?expired|"
                           r"invalid api key|authentication|usage limit|limit reached|hit your limit|"
                           r"resets? (?:at )?\d|out of (?:extra )?usage|rate.?limit|overloaded|credit balance|"
                           r"model.*(?:not found|not available|does not exist)|permission_error|"
                           r"connection (?:error|refused|reset)|network error|request timed out")
# Output from claude itself, not a reply: an option this version does not know, and the like.
CLI_ERROR = re.compile(r"(?i)unknown option|unknown argument|invalid (?:value|argument)|error: option|not logged in")
SYSTEMIC_STATUS_HERE = SYSTEMIC_STATUS | {404, 408}  # 404: the model is unknown or not available


class ClaudeCodeError(Exception):
    """A failed `claude -p` call. systemic: the login, the binary or the service is the problem."""

    def __init__(self, message, systemic):
        super().__init__(message)
        self.systemic = systemic


def child_env(env=None, max_tokens=MAX_TOKENS):
    """The environment for `claude -p`: the caller's, without an API key or auth token."""
    env = dict(os.environ if env is None else env)
    for name in STRIPPED_ENV:
        env.pop(name, None)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    return env


def command(binary, model, effort, system_prompt_file):
    return [binary, "-p",
            "--model", model,
            "--effort", effort,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True}),
            "--system-prompt-file", system_prompt_file,
            "--output-format", "json",
            "--json-schema", json.dumps(OUTPUT_SCHEMA)]


def flatten(messages):
    """API-style messages -> one prompt. A repair request carries the bad reply along with it."""
    if len(messages) == 1:
        return messages[0]["content"]
    first, *rest = messages
    parts = [first["content"]]
    for m in rest:
        heading = "# Your previous reply" if m["role"] == "assistant" else "# Request"
        parts.append(f"{heading}\n{m['content']}")
    return "\n\n".join(parts)


def _systemic_reply(out):
    status = out.get("api_error_status")
    text = SYSTEMIC_TEXT.search(str(out.get("result") or ""))
    if isinstance(status, int):
        # A 400 can be about billing or permissions too ("credit balance is too low").
        return status in SYSTEMIC_STATUS_HERE or status >= 500 or bool(text)
    return out.get("terminal_reason") == "api_error" or bool(text)


class ClaudeCodeExtractor(Extractor):
    """Calls Haiku through `claude -p` on the user's Claude Code login. Same contract as Extractor."""

    def __init__(self, model="claude-haiku-5-5", effort="low", timeout=DEFAULT_TIMEOUT, binary=None, env=None):
        """env: the whole environment for the child (default os.environ), before the stripping."""
        self.client, self.model, self.effort = None, model, effort
        self.timeout, self.env = timeout, env
        self.binary = binary or os.environ.get("FACTS_CLAUDE_BIN") or "claude"
        # Not Extractor.__init__: that builds an API client. Same version string: same model and prompt.
        self.version = f"{model}+{PROMPT_VERSION}"

    def _run(self, prompt, max_tokens):
        """One `claude -p` process -> its parsed JSON output. Raises ClaudeCodeError."""
        binary = shutil.which(self.binary)
        if not binary:
            raise ClaudeCodeError(f"Claude Code ({self.binary}) is not installed or not on PATH", systemic=True)
        with tempfile.TemporaryDirectory(prefix="facts-claude-") as cwd:
            prompt_file = os.path.join(cwd, "system-prompt.md")
            with open(prompt_file, "w") as f:
                f.write(SYSTEM_PROMPT)
            proc = subprocess.Popen(command(binary, self.model, self.effort, prompt_file), cwd=cwd,
                                    env=child_env(self.env, max_tokens), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = proc.communicate(prompt.encode(), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
                # Systemic, as an API timeout is: a hung service must not cost the exchange an attempt.
                raise ClaudeCodeError(f"claude -p timed out after {self.timeout:g} s", systemic=True) from None
            except BaseException:  # interrupted: never leave a claude running behind us
                _kill(proc)
                raise
        try:
            out = json.loads(stdout)
            if not isinstance(out, dict):
                raise ValueError("not an object")
        except ValueError:
            detail = (stderr or stdout).decode(errors="replace").strip()
            # Not runnable (126/127), a CLI that rejects our options, or no login: the install is
            # the problem. Any other crash may be this exchange's doing, so it counts as an attempt.
            systemic = proc.returncode in (126, 127) or bool(CLI_ERROR.search(detail) or SYSTEMIC_TEXT.search(detail))
            raise ClaudeCodeError(f"claude -p exited {proc.returncode} without JSON output: {detail[:200]}",
                                  systemic=systemic) from None
        return out

    def _call(self, messages, calls, max_tokens=MAX_TOKENS):
        t = time.perf_counter()
        out = self._run(flatten(messages), max_tokens)
        u = out.get("usage") or {}
        structured = out.get("structured_output")
        text = json.dumps(structured) if isinstance(structured, dict) else str(out.get("result") or "")
        stop = out.get("stop_reason")
        if out.get("is_error") and stop != "refusal":
            stop = "error"
        calls.append({"uncached_in": u.get("input_tokens"), "cache_read": u.get("cache_read_input_tokens"),
                      "cache_write": u.get("cache_creation_input_tokens"), "output_tokens": u.get("output_tokens"),
                      "seconds": time.perf_counter() - t, "stop": stop, "response": text,
                      "messages": messages})
        if stop == "error":
            message = f"claude -p failed ({out.get('subtype')}): {str(out.get('result') or '')[:200]}"
            if _systemic_reply(out) or max_tokens > MAX_TOKENS:
                # A systemic failure is never retried here; a content failure is retried once
                # (Extractor.extract retries an empty reply with twice the cap), then recorded.
                raise ClaudeCodeError(message, systemic=_systemic_reply(out))
            return "", stop
        return text, stop


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: claude may have children
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # a descendant that left the group still holds the pipes; give up on them


def _positive(env, name, default, cast):
    raw = (os.environ if env is None else env).get(name)
    if not raw:
        return default
    value = cast(raw)
    if value <= 0:
        raise ValueError(f"{name}={raw!r}: must be greater than 0")
    return value


def workers(env=None):
    """Sessions the eval harness may run side by side: $FACTS_CLAUDE_CODE_WORKERS, default 3."""
    return _positive(env, "FACTS_CLAUDE_CODE_WORKERS", DEFAULT_WORKERS, int)


def timeout(env=None):
    """Seconds per claude -p call: $FACTS_CLAUDE_TIMEOUT_SECONDS, default 120."""
    return _positive(env, "FACTS_CLAUDE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, float)
