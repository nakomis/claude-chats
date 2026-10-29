# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""The single-machine side of facts (CHATS-39): providers, the hook embedder, the runner. No network.

The distributed edition's defaults are asserted too: on Mace nothing here may change.
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "hook"))  # the hook package, for hook.embed and hook.drain

from facts import claude_code, extract, service, single_machine  # noqa: E402


def test_the_distributed_defaults_are_unchanged():
    cfg = service.Config.from_env({"FACTS_EMBED_URL": "http://cal:11434"})
    assert (cfg.provider, cfg.embedder, cfg.effort, cfg.priority_since) == ("anthropic", "ollama", "low", "")
    assert (cfg.embed_model, cfg.embed_tag, cfg.embed_max_chars) == ("mxbai-q8", "ollama-mxbai-q8", 800)


@pytest.fixture
def fake_anthropic(monkeypatch):
    """A stand-in anthropic module: records how each client was built."""
    built = []

    class Client:
        def __init__(self, **kw):
            built.append((type(self).__name__, kw))

    mod = types.ModuleType("anthropic")
    mod.Anthropic = type("Anthropic", (Client,), {})
    mod.AnthropicBedrock = type("AnthropicBedrock", (Client,), {})
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return built


def test_bedrock_uses_the_aws_settings_and_an_inference_profile(fake_anthropic):
    cfg = service.Config.from_env({"FACTS_PROVIDER": "bedrock"})
    ex = service.make_extractor(cfg, env={"AWS_PROFILE": "facts", "AWS_DEFAULT_REGION": "eu-west-2"})
    (name, kw), = fake_anthropic
    assert name == "AnthropicBedrock" and kw["aws_profile"] == "facts" and kw["aws_region"] == "eu-west-2"
    assert ex.model == service.BEDROCK_DEFAULT_MODEL == "global.anthropic.claude-haiku-5-5"
    assert ex.version == "global.anthropic.claude-haiku-5-5+facts-v1" and ex.effort == "low"

    fake_anthropic.clear()
    cfg = service.Config.from_env({"FACTS_PROVIDER": "bedrock", "FACTS_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
                                   "FACTS_EFFORT": "none"})
    ex = service.make_extractor(cfg, env={})
    assert fake_anthropic[0][1]["aws_profile"] is None and fake_anthropic[0][1]["aws_region"] is None
    assert ex.model.startswith("eu.anthropic.claude-haiku-4-5") and ex.effort is None


def test_no_effort_means_no_output_config():
    seen = []

    class Client:
        messages = types.SimpleNamespace(create=lambda **kw: seen.append(kw) or types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text='{"memory": []}')], stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=1, cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0, output_tokens=1)))

    extract.Extractor(Client(), effort=None).extract([{"role": "user", "content": "x"}], "2026-10-29")
    extract.Extractor(Client()).extract([{"role": "user", "content": "x"}], "2026-10-29")
    assert "output_config" not in seen[0] and seen[1]["output_config"] == {"effort": "low"}


def test_claude_code_and_unknown_providers(fake_anthropic):
    ex = service.make_extractor(service.Config(provider="claude-code"), env={})
    assert isinstance(ex, claude_code.ClaudeCodeExtractor) and fake_anthropic == []
    with pytest.raises(service.ConfigError):
        service.make_extractor(service.Config(provider="ollama"))


@pytest.fixture
def hook_embed(monkeypatch):
    from hook import embed

    monkeypatch.setattr(embed, "PROVIDER", "openai")
    monkeypatch.setattr(embed, "MODEL", "text-embedding-3-small")
    asked = []
    monkeypatch.setattr(embed, "get_embedding", lambda text, for_query=False: asked.append(text) or [0.1] * 1024)
    return asked


def test_the_hook_embedder_is_the_one_queries_use(hook_embed):
    cfg, embed = service.make_hook_embedder(service.Config(embedder="hook"), env={})
    assert cfg.embed_tag == "openai-text-embedding-3-small"
    assert len(embed("Rey is a Pi")) == 1024 and hook_embed == ["Rey is a Pi"]
    cfg, _ = service.make_hook_embedder(service.Config(embedder="hook"), env={"FACTS_EMBED_TAG": "mine"})
    assert cfg.embed_tag == "ollama-mxbai-q8"  # an explicit FACTS_EMBED_TAG is left as Config read it
    cfg, embed = service.make_hook_embedder(service.Config(embedder="hook", embed_dims=768), env={})
    with pytest.raises(ValueError, match="1024 dimensions"):
        embed("x")


def _main(monkeypatch, env, argv=()):
    """service.main with the database and the run faked; returns what run() was given."""
    for k in list(os.environ):
        if k.startswith(("FACTS_", "CM_DATABASE_URL", "OLLAMA_BASE_URL")):
            monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    got = {}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(service, "connect", lambda cfg: Conn())
    monkeypatch.setattr(service, "make_extractor", lambda cfg: "extractor")
    monkeypatch.setattr(service, "make_embedder", lambda cfg: "cal's ollama")
    monkeypatch.setattr(service, "run", lambda conn, cfg, ex, embed, limit, dry: got.update(cfg=cfg, embed=embed))
    service.main(list(argv))
    return got


def test_main_embeds_through_the_hook_without_an_ollama_url(monkeypatch, hook_embed):
    got = _main(monkeypatch, {"FACTS_DATABASE_URL": "postgresql://x/y", "FACTS_SINCE": "all",
                              "FACTS_EMBEDDER": "hook", "FACTS_PROVIDER": "claude-code"})
    assert got["cfg"].embed_tag == "openai-text-embedding-3-small" and got["embed"]("t") == [0.1] * 1024


def test_main_still_needs_cals_url_in_the_distributed_edition(monkeypatch):
    with pytest.raises(SystemExit, match="FACTS_EMBED_URL"):
        _main(monkeypatch, {"FACTS_DATABASE_URL": "postgresql://x/y", "FACTS_SINCE": "all"})
    got = _main(monkeypatch, {"FACTS_DATABASE_URL": "postgresql://x/y", "FACTS_SINCE": "all",
                              "FACTS_EMBED_URL": "http://cal:11434"})
    assert got["cfg"].embedder == "ollama" and got["cfg"].embed_tag == "ollama-mxbai-q8"
    assert got["embed"] == "cal's ollama"


def test_main_does_nothing_when_off(monkeypatch):
    assert _main(monkeypatch, {"FACTS_PROVIDER": "off"}) == {}


def test_single_machine_settings_fill_in_but_never_override():
    env = single_machine.single_machine_env({
        "CLAUDE_CHATS_DB_URL": "postgresql://one/db", "CLAUDE_CHATS_DIMENSIONS": "1024",
        "FACTS_INSTALLED_AT": "2026-10-29T11:00:00+00:00", "FACTS_EMBED_DIMS": "768"})
    assert env["FACTS_PROVIDER"] == "claude-code" and env["FACTS_DATABASE_URL"] == "postgresql://one/db"
    assert env["FACTS_EMBEDDER"] == "hook" and env["FACTS_EMBED_DIMS"] == "768"
    assert env["FACTS_PRIORITY_SINCE"] == "2026-10-29T11:00:00+00:00"


def test_one_run_at_a_time(tmp_path):
    first = single_machine.take_lock(tmp_path / "facts.lock")
    assert first is not None
    assert single_machine.take_lock(tmp_path / "facts.lock") is None
    first.close()
    assert single_machine.take_lock(tmp_path / "facts.lock") is not None


def _runner(monkeypatch, tmp_path, config, argv=()):
    for k in list(os.environ):
        if k.startswith(("FACTS_", "CLAUDE_CHATS_", "ANTHROPIC_")):
            monkeypatch.delenv(k)
    (tmp_path / "config.env").write_text(config)
    ran = []
    monkeypatch.setattr(service, "main", lambda argv: ran.append((argv, dict(os.environ))))
    single_machine.main(["--env-file", str(tmp_path / "config.env"), *argv])
    return ran


def test_the_runner_reads_the_config_and_hands_over(monkeypatch, tmp_path):
    ran = _runner(monkeypatch, tmp_path, "CLAUDE_CHATS_DB_URL=postgresql://one/db\nFACTS_SINCE=2026-10-29\n",
                  ["--limit", "3"])
    (argv, env), = ran
    assert argv == ["--limit", "3"]
    assert env["FACTS_DATABASE_URL"] == "postgresql://one/db" and env["FACTS_EMBEDDER"] == "hook"
    assert (tmp_path / "facts.lock").exists()


def test_the_runner_skips_while_another_run_holds_the_lock(monkeypatch, tmp_path, capsys):
    held = single_machine.take_lock(tmp_path / "facts.lock")
    assert _runner(monkeypatch, tmp_path, "FACTS_PROVIDER=claude-code\n") == []
    assert "another run" in capsys.readouterr().err
    held.close()


def test_the_runner_does_nothing_when_off(monkeypatch, tmp_path):
    assert _runner(monkeypatch, tmp_path, "FACTS_PROVIDER=off\n") == []
    assert not (tmp_path / "facts.lock").exists()


def test_the_runner_wants_a_key_for_the_anthropic_provider(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        _runner(monkeypatch, tmp_path, "FACTS_PROVIDER=anthropic\n")
