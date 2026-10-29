# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""Unit tests for facts/claude_code.py against a fake `claude` on PATH. No real Claude Code calls."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facts import claude_code, extract, service  # noqa: E402

# Records each call (argv, cwd, environment, stdin) and plays back the queued replies in order.
FAKE_CLAUDE = f"""#!{sys.executable}
import json, os, sys, time
d = os.environ["FAKE_CLAUDE_DIR"]
n = len([f for f in os.listdir(d) if f.startswith("call-")])
stdin = sys.stdin.read()
with open(os.path.join(d, f"call-{{n}}.json"), "w") as f:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "stdin": stdin,
               "cwd_files": sorted(os.listdir("."))}}, f)
reply = json.load(open(os.path.join(d, "replies.json")))[n]
time.sleep(reply.get("sleep", 0))
sys.stderr.write(reply.get("stderr", ""))
sys.stdout.write(reply["stdout"] if isinstance(reply.get("stdout"), str) else json.dumps(reply["out"]))
sys.exit(reply.get("exit", 0))
"""


def ok(memory=None, structured=True, **over):
    """A successful `claude -p --output-format json` result, as Claude Code 2.1.312 prints it."""
    body = {"memory": memory if memory is not None else []}
    out = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "stop_reason": "tool_use",
           "terminal_reason": "completed", "api_error_status": None, "result": json.dumps(body),
           "usage": {"input_tokens": 3, "cache_read_input_tokens": 1100, "cache_creation_input_tokens": 90,
                     "output_tokens": 40},
           "total_cost_usd": 0.0003}
    if structured:
        out["structured_output"] = body
    out.update(over)
    return {"out": out}


def error(result, **over):
    out = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "stop_reason": "stop_sequence",
           "terminal_reason": "api_error", "api_error_status": None, "result": result,
           "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                     "output_tokens": 0}}
    out.update(over)
    return {"out": out, "exit": 1}


FACT = {"id": "0", "text": "Rey is a Raspberry Pi at 192.0.2.36.", "kind": "config", "subject": "Rey",
        "attribute": "ip address"}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Puts a fake `claude` first on PATH; returns (queue replies, read recorded calls)."""
    bin_dir, spool = tmp_path / "bin", tmp_path / "spool"
    bin_dir.mkdir()
    spool.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(spool))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-must-not-reach-the-child")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-token-must-not-reach-the-child")
    monkeypatch.delenv("FACTS_CLAUDE_BIN", raising=False)

    def queue(*replies):
        (spool / "replies.json").write_text(json.dumps(list(replies)))

    def calls():
        return [json.loads((spool / f"call-{i}.json").read_text())
                for i in range(len(list(spool.glob("call-*.json"))))]

    return queue, calls


def run(ex=None):
    ex = ex or claude_code.ClaudeCodeExtractor(timeout=10)
    return ex.extract([{"role": "user", "content": "Rey is at 192.0.2.36"}], "2026-10-29")


def test_facts_come_from_structured_output_and_usage_is_mapped(fake):
    queue, calls = fake
    queue(ok([FACT]))
    res = run()
    assert res.error is None and not res.systemic
    assert [f.text for f in res.facts] == [FACT["text"]] and res.facts[0].attribute == "ip address"
    (c,) = res.calls
    assert (c["uncached_in"], c["cache_read"], c["cache_write"], c["output_tokens"]) == (3, 1100, 90, 40)
    assert c["stop"] == "tool_use" and json.loads(c["response"])["memory"][0]["id"] == "0"
    assert c["seconds"] > 0 and c["messages"][0]["role"] == "user"
    assert len(calls()) == 1


def test_the_child_is_isolated_and_never_sees_an_api_key(fake):
    queue, calls = fake
    queue(ok())
    run()
    (c,) = calls()
    argv, env = c["argv"], c["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["FAKE_CLAUDE_DIR"]  # the rest of the environment is passed through
    assert argv[0] == "-p" and "--bare" not in argv
    for flag in ("--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--effort") + 1] == "low"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["memory"]["items"]["properties"]["kind"]["enum"] == list(extract.KINDS)
    # The system prompt goes by file, the exchange on stdin, from an empty scratch directory.
    prompt_file = Path(argv[argv.index("--system-prompt-file") + 1])
    assert prompt_file.name == "system-prompt.md" and prompt_file.parent.resolve() == Path(c["cwd"]).resolve()
    assert c["cwd_files"] == ["system-prompt.md"]
    assert "## New Messages" in c["stdin"] and "2026-10-29" in c["stdin"]
    assert not Path(c["cwd"]).exists()  # cleaned up afterwards


def test_result_text_is_parsed_when_structured_output_is_missing(fake):
    queue, _ = fake
    queue(ok([FACT], structured=False))
    res = run()
    assert res.error is None and [f.subject for f in res.facts] == ["Rey"]


def test_credential_shaped_facts_are_still_dropped(fake):
    queue, _ = fake
    queue(ok([{**FACT, "text": "token = sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}]))  # secret-check:allow (fake)
    res = run()
    assert res.facts == [] and res.dropped == ["looks like a credential"]


def test_a_content_error_is_retried_once_with_a_bigger_cap(fake):
    queue, calls = fake
    queue(error("structured output failed", terminal_reason="completed", subtype="error_max_structured_output_retries"),
          ok([FACT]))
    res = run()
    assert res.error is None and len(res.facts) == 1
    assert [c["stop"] for c in res.calls] == ["error", "tool_use"]
    caps = [c["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] for c in calls()]
    assert caps == [str(extract.MAX_TOKENS), str(2 * extract.MAX_TOKENS)]


def test_two_content_errors_are_recorded_not_systemic(fake):
    queue, _ = fake
    bad = error("structured output failed", terminal_reason="completed", subtype="error_during_execution")
    queue(bad, bad)
    res = run()
    assert res.facts == [] and "claude -p failed" in res.error and not res.systemic and len(res.calls) == 2


def test_a_refusal_is_recorded_not_retried(fake):
    queue, calls = fake
    queue(ok(structured=False, stop_reason="refusal", result=""))
    res = run()
    assert res.error == "refused" and len(calls()) == 1


def test_invalid_json_gets_one_repair_call_carrying_the_bad_reply(fake):
    queue, calls = fake
    queue(ok(structured=False, result='{"memory": [{"id": "0", "text": "a"'), ok([FACT]))
    res = run()
    assert res.error is None and len(res.facts) == 1
    second = calls()[1]["stdin"]
    assert "# Your previous reply" in second and "not the JSON asked for" in second and "## New Messages" in second


@pytest.mark.parametrize("reply", [
    error("Not logged in · Please run /login"),
    error("Claude AI usage limit reached"),
    error("API Error: 529 overloaded", api_error_status=529),
    {"stdout": "", "stderr": "unknown option '--json-schema'", "exit": 2},
])
def test_login_limit_and_cli_failures_are_systemic_and_not_retried(fake, reply):
    queue, calls = fake
    queue(reply, ok([FACT]))
    res = run()
    assert res.systemic and res.facts == [] and len(calls()) == 1


def test_a_bad_request_is_not_systemic(fake):
    queue, _ = fake
    bad = error("API Error: 400 prompt is too long", api_error_status=400)
    queue(bad, bad)
    assert run().systemic is False


def test_a_missing_binary_is_systemic(fake):
    res = run(claude_code.ClaudeCodeExtractor(binary="no-such-claude-binary-here"))
    assert res.systemic and "not installed" in res.error and res.calls == []


def test_a_hung_child_is_killed_and_the_timeout_is_systemic(fake):
    queue, _ = fake
    queue({**ok([FACT]), "sleep": 30})
    ex = claude_code.ClaudeCodeExtractor(timeout=0.5)
    res = run(ex)
    assert res.systemic and "timed out" in res.error


def test_the_service_picks_the_provider_from_its_config():
    cfg = service.Config.from_env({"FACTS_PROVIDER": "claude-code", "FACTS_API_TIMEOUT_SECONDS": "33"})
    ex = service.make_extractor(cfg, env={})
    assert isinstance(ex, claude_code.ClaudeCodeExtractor) and ex.timeout == 33
    assert service.Config.from_env({}).provider == "anthropic"
    with pytest.raises(service.ConfigError):
        service.make_extractor(service.Config(provider="openai"))


def test_harness_factories(monkeypatch):
    monkeypatch.setenv("FACTS_PROVIDER", "claude-code")
    monkeypatch.setenv("FACTS_CLAUDE_CODE_WORKERS", "2")
    adapter = extract.make_extractor(None)
    assert isinstance(adapter.extractor, claude_code.ClaudeCodeExtractor)
    assert adapter.parallel_workers == 2 and adapter.name.startswith("facts:claude-code:")
    monkeypatch.delenv("FACTS_CLAUDE_CODE_WORKERS")
    assert extract.make_claude_code_extractor(None).parallel_workers == claude_code.DEFAULT_WORKERS
    monkeypatch.setenv("FACTS_PROVIDER", "nonsense")
    with pytest.raises(ValueError):
        extract.make_extractor(None)
