"""localhost/install.sh, run for real against a throwaway HOME.

Every external command it reaches for — docker, uv, claude, ollama, launchctl,
systemctl, plutil, even uname — is a stub on a PATH that holds nothing else of
note, so nothing here can touch the real ~/.claude, Claude Code's MCP
registry, launchd or a database. The stubs log their arguments, and the tests
read the logs and the files written under the fake HOME.

The cases that matter are the upgrades (CHATS-36): an install made from an old
checkout ran a copy of the hook under localhost/hook, possibly in the removed
`direct` mode with its database URL on the hook command line, and re-running
the installer has to leave nothing pointing at any of that, while keeping
every byte of data it finds.
"""

import json
import os
import plistlib
import shlex
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "localhost" / "install.sh"

STUB = """#!/bin/bash
echo "{name} $*" >> "$STUB_LOG"
{body}
exit 0
"""
STUB_BODIES = {
    # `docker exec -i … psql` is the schema being applied: keep what it was fed.
    "docker": """if [[ "$1 $2" == "exec -i" ]]; then
  cat > "$STUB_DIR/applied.sql"
  echo apply >> "$STUB_DIR/applies"
  if [[ -n "${APPLY_FAILS:-}" && $(wc -l < "$STUB_DIR/applies") -le ${APPLY_FAILS} ]]; then
    echo "ERROR:  ${APPLY_ERROR:-canceling statement due to lock timeout}" >&2; exit 3
  fi
fi""",
    # Claims the model is present, so nothing is "pulled".
    "ollama": '[[ "$1" == list ]] && echo "mxbai-embed-large:latest  abc  669 MB"',
    "uname": 'echo "$FAKE_UNAME"; exit 0',
}

# install.sh asks `claude auth status --json` from a launchd-like empty environment,
# so this stub finds its directory from its own path, not STUB_DIR. Logged in unless
# the test wrote "false" to claude-logged-in; records what environment it saw.
CLAUDE_STUB = """#!/bin/bash
dir="$(cd "$(dirname "$0")/.." && pwd)"
echo "claude $*" >> "$dir/calls.log"
if [[ "$1 $2" == "auth status" ]]; then
  echo "key=${ANTHROPIC_API_KEY:-none} path=$PATH stub_dir=${STUB_DIR:-none}" >> "$dir/auth-env"
  echo "{\\"loggedIn\\": $(cat "$dir/claude-logged-in" 2>/dev/null || echo true)}"
fi
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("docker", "uv", "claude", "ollama", "launchctl", "systemctl", "plutil", "uname"):
        f = bindir / name
        body = STUB_BODIES.get(name, "")
        if name == "uname":
            f.write_text(f"#!/bin/bash\n{body}\n")
        elif name == "claude":
            f.write_text(CLAUDE_STUB)
        else:
            f.write_text(STUB.format(name=name, body=body))
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    # install.sh needs Python 3.11+, which /usr/bin/python3 may not be.
    (bindir / "python3").symlink_to(sys.executable)
    return tmp_path, home, bindir


def _run(sandbox, *args, uname="Darwin", extra_env=None, logged_in=True):
    tmp_path, home, bindir = sandbox
    (tmp_path / "claude-logged-in").write_text("true" if logged_in else "false")
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_DIR": str(tmp_path),
        "FAKE_UNAME": uname,
        "TERM": "dumb",
        **(extra_env or {}),
    }
    return subprocess.run(["bash", str(INSTALL), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _calls(sandbox, prefix):
    log = sandbox[0] / "calls.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if line.startswith(prefix + " ")]


def _settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def _hook_commands(settings):
    return {
        event: [h["command"] for g in groups for h in g["hooks"]]
        for event, groups in settings.get("hooks", {}).items()
    }


OLD_HOOK = ("CLAUDE_CHATS_CAPTURE_MODE=direct "
            "CLAUDE_CHATS_DB_URL=postgresql://claude:claude@localhost:5433/claude_chats "
            "CLAUDE_CHATS_PROVIDER=ollama OLLAMA_BASE_URL=http://localhost:11434 "
            'uv run --project "/old/checkout/localhost/hook" record-conversation')
OTHER_HOOK = {"matcher": "", "hooks": [{"type": "command", "command": "echo unrelated"}]}


def _seed_old_install(home):
    """What an install from before CHATS-36 left behind."""
    (home / ".claude").mkdir()
    old_group = {"matcher": "", "hooks": [{"type": "command", "command": OLD_HOOK, "timeout": 120}]}
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [OTHER_HOOK, old_group], "UserPromptSubmit": [old_group],
                  "SessionEnd": [old_group]},
    }))
    state = home / ".claude-chats"
    state.mkdir()
    (state / "config.env").write_text(
        "CLAUDE_CHATS_CAPTURE_MODE=direct\nCLAUDE_CHATS_PROVIDER=ollama\n"
        "CLAUDE_CHATS_MODEL=mxbai-embed-large\nCLAUDE_CHATS_DIMENSIONS=1024\n"
        "OLLAMA_BASE_URL=http://ollama.example:11434\n")
    (state / "outbox.db").write_bytes(b"SQLite format 3\x00 pretend outbox")
    (state / "outbox-fallback.ndjson").write_text('{"message_uuid": "x"}\n')
    images = state / "images" / "sess"
    images.mkdir(parents=True)
    (images / "a.png").write_bytes(b"\x89PNG")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.nakomis.claude-chats-drain.plist").write_text("<old/>")


def _assert_installed(sandbox, result):
    _, home, _ = sandbox
    assert result.returncode == 0, result.stdout + result.stderr

    commands = _hook_commands(_settings(home))
    for event in ("Stop", "UserPromptSubmit", "SessionEnd"):
        ours = [c for c in commands[event] if "record-conversation" in c]
        assert len(ours) == 1, (event, commands[event])
        words = shlex.split(ours[0])
        assert words[0] == "CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1"
        # Optionally SSL_CERT_FILE, the one setting the hook process itself uses.
        words = [w for w in words[1:] if not w.startswith("SSL_CERT_FILE=")]
        assert words == ["uv", "run", "--project", str(ROOT / "hook"), "record-conversation"]

    flat = json.dumps(_settings(home))
    for stale in ("localhost/hook", "CAPTURE_MODE", "CLAUDE_CHATS_DB_URL"):
        assert stale not in flat

    # The log spans every run in the test; the latest registration is what counts.
    add = [c for c in _calls(sandbox, "claude") if c.startswith("claude mcp add")][-1]
    assert add.endswith(f"-- uv run --project {ROOT / 'mcp'} conversation-memory-mcp")
    assert "localhost/mcp" not in add
    # One database and no replica (CHATS-38): recall_facts reads the primary.
    for written in (add, json.dumps(_settings(home)),
                    (home / ".claude-chats" / "config.env").read_text()):
        assert "READ_DB_URL" not in written

    config = home / ".claude-chats" / "config.env"
    assert "CAPTURE_MODE" not in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    assert (sandbox[0] / "applied.sql").read_text() == (ROOT / "init.sql").read_text()
    assert any(f"-f {ROOT / 'localhost' / 'docker-compose.yml'} up -d" in c
               for c in _calls(sandbox, "docker"))
    assert f"uv sync --project {ROOT / 'hook'}" in _calls(sandbox, "uv")
    assert f"uv sync --project {ROOT / 'mcp'}" in _calls(sandbox, "uv")
    assert f"uv sync --project {ROOT / 'localhost' / 'facts'}" in _calls(sandbox, "uv")


def _config(home):
    lines = (home / ".claude-chats" / "config.env").read_text().splitlines()
    return dict(ln.split("=", 1) for ln in lines if ln and not ln.startswith("#"))


FACTS_PLIST = "com.nakomis.claude-chats-facts.plist"


def test_fresh_install_on_macos(sandbox):
    _, home, bindir = sandbox
    result = _run(sandbox)
    _assert_installed(sandbox, result)

    plist = home / "Library" / "LaunchAgents" / "com.nakomis.claude-chats-drain.plist"
    args = plistlib.loads(plist.read_bytes())["ProgramArguments"]
    assert args == [str(bindir / "uv"), "run", "--project", str(ROOT / "hook"), "drain-outbox",
                    "--env-file", str(home / ".claude-chats" / "config.env")]
    assert any(c.startswith("launchctl bootstrap gui/") for c in _calls(sandbox, "launchctl"))
    assert _calls(sandbox, "systemctl") == []


def test_fresh_install_on_linux(sandbox):
    _, home, bindir = sandbox
    result = _run(sandbox, uname="Linux")
    _assert_installed(sandbox, result)

    units = home / ".config" / "systemd" / "user"
    service = (units / "claude-chats-drain.service").read_text()
    assert (f'ExecStart="{bindir / "uv"}" run --project "{ROOT / "hook"}" drain-outbox '
            f'--env-file "{home / ".claude-chats" / "config.env"}"') in service
    assert (units / "claude-chats-drain.timer").exists()
    assert "systemctl --user enable --now claude-chats-drain.timer" in _calls(sandbox, "systemctl")
    assert _calls(sandbox, "launchctl") == []
    assert not (home / "Library").exists()


def test_upgrading_an_old_direct_mode_install(sandbox):
    _, home, _ = sandbox
    _seed_old_install(home)
    state = home / ".claude-chats"
    before = {p: p.read_bytes() for p in (state / "outbox.db", state / "outbox-fallback.ndjson",
                                          state / "images" / "sess" / "a.png")}

    result = _run(sandbox)
    _assert_installed(sandbox, result)
    assert "direct capture mode, which has been removed" in result.stdout
    assert "--replay-fallback" in result.stdout  # the fallback file is pointed out

    # Settings that are not ours survive, in place.
    settings = _settings(home)
    assert settings["model"] == "opus"
    assert "echo unrelated" in _hook_commands(settings)["Stop"]
    # Remembered provider settings carry over.
    assert "OLLAMA_BASE_URL=http://ollama.example:11434" in (state / "config.env").read_text()
    # Not a byte of data touched.
    assert {p: p.read_bytes() for p in before} == before
    plist = (home / "Library" / "LaunchAgents" / "com.nakomis.claude-chats-drain.plist").read_text()
    assert "drain-outbox" in plist and str(ROOT / "hook") in plist


def test_re_running_changes_nothing(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox))
    first = (home / ".claude" / "settings.json").read_text()
    _assert_installed(sandbox, _run(sandbox))
    assert (home / ".claude" / "settings.json").read_text() == first


def test_direct_mode_is_refused(sandbox):
    result = _run(sandbox, "--mode", "direct")
    assert result.returncode != 0
    assert "Direct capture mode has been removed" in result.stderr
    assert not (sandbox[1] / ".claude").exists()


def test_uninstall_removes_ours_and_keeps_the_rest(sandbox):
    _, home, _ = sandbox
    _seed_old_install(home)
    assert _run(sandbox).returncode == 0
    result = _run(sandbox, "--uninstall")
    assert result.returncode == 0, result.stderr

    settings = _settings(home)
    assert "record-conversation" not in json.dumps(settings)
    assert _hook_commands(settings) == {"Stop": ["echo unrelated"]}
    assert not (home / "Library" / "LaunchAgents" / "com.nakomis.claude-chats-drain.plist").exists()
    assert (home / ".claude-chats" / "outbox.db").exists()
    assert any(c.startswith("claude mcp remove conversation-memory") for c in _calls(sandbox, "claude"))


def test_backup_and_restore_use_the_shared_scripts():
    text = INSTALL.read_text()
    assert '"${ROOT_DIR}/scripts/backup.sh" --local' in text
    assert '"${ROOT_DIR}/scripts/restore.sh" --local' in text
    assert not (ROOT / "localhost" / "scripts").exists()


def test_everything_written_stays_inside_home(sandbox):
    tmp_path, home, _ = sandbox
    before = set(tmp_path.rglob("*"))
    assert _run(sandbox).returncode == 0
    written = {p for p in set(tmp_path.rglob("*")) - before}
    outside = sorted(str(p) for p in written
                     if home not in p.parents and p.name not in ("calls.log", "applied.sql", "applies", "auth-env",
                                                              "claude-logged-in"))
    assert outside == []


def test_a_lock_timeout_applying_init_sql_is_retried(sandbox):
    result = _run(sandbox, extra_env={"APPLY_FAILS": "1"})
    _assert_installed(sandbox, result)
    assert (sandbox[0] / "applies").read_text().count("apply") == 2
    assert "retrying" in result.stdout


def test_any_other_schema_error_stops_the_install(sandbox):
    result = _run(sandbox, extra_env={"APPLY_FAILS": "9", "APPLY_ERROR": "syntax error"})
    assert result.returncode != 0
    assert "syntax error" in result.stderr
    assert (sandbox[0] / "applies").read_text().count("apply") == 1
    assert not (sandbox[1] / ".claude").exists()


def test_invalid_settings_json_changes_nothing(sandbox):
    _, home, _ = sandbox
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"hooks": {,}')
    result = _run(sandbox)
    assert result.returncode != 0
    assert "not valid JSON" in result.stderr
    assert (home / ".claude" / "settings.json").read_text() == '{"hooks": {,}'
    assert _calls(sandbox, "claude") == [] and _calls(sandbox, "docker") == []
    assert not (home / ".claude-chats").exists()


def test_settings_keep_shared_groups_unicode_and_indentation(sandbox):
    _, home, _ = sandbox
    (home / ".claude").mkdir()
    original = json.dumps({
        "statusLine": "café ✓",
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "echo mine"},
            {"type": "command", "command": OLD_HOOK},
        ]}]},
    }, indent=4, ensure_ascii=False) + "\n"
    (home / ".claude" / "settings.json").write_text(original, encoding="utf-8")

    _assert_installed(sandbox, _run(sandbox))
    text = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert "café ✓" in text
    assert '\n    "hooks": {' in text  # still four-space indented
    stop = json.loads(text)["hooks"]["Stop"]
    assert stop[0]["hooks"] == [{"type": "command", "command": "echo mine"}]
    assert (home / ".claude" / "settings.json.bak").read_text(encoding="utf-8") == original


def test_config_is_private_even_when_it_was_not(sandbox):
    _, home, _ = sandbox
    state = home / ".claude-chats"
    state.mkdir()
    (state / "config.env").write_text("CLAUDE_CHATS_PROVIDER=ollama\n")
    (state / "config.env").chmod(0o644)
    _assert_installed(sandbox, _run(sandbox))  # asserts mode 600
    assert not list(state.glob("config.env.tmp*"))


def test_bedrock_and_tls_settings_survive_a_re_run(sandbox):
    _, home, _ = sandbox
    first = _run(sandbox, extra_env={
        "CLAUDE_CHATS_PROVIDER": "bedrock", "AWS_PROFILE": "convmem",
        "AWS_REGION": "eu-west-2", "SSL_CERT_FILE": "/etc/ssl/house.pem"})
    _assert_installed(sandbox, first)
    _assert_installed(sandbox, _run(sandbox))  # nothing in the environment this time
    config = (home / ".claude-chats" / "config.env").read_text()
    for line in ("CLAUDE_CHATS_PROVIDER=bedrock", "AWS_PROFILE=convmem",
                 "AWS_REGION=eu-west-2", "SSL_CERT_FILE=/etc/ssl/house.pem"):
        assert line in config
    add = [c for c in _calls(sandbox, "claude") if c.startswith("claude mcp add")][-1]
    assert "AWS_PROFILE=convmem" in add and "SSL_CERT_FILE=/etc/ssl/house.pem" in add


# ── Claude Facts (CHATS-39) ─────────────────────────────────────────────────────

def test_facts_default_to_the_claude_code_login_on_macos(sandbox):
    _, home, bindir = sandbox
    result = _run(sandbox, extra_env={"ANTHROPIC_API_KEY": "sk-not-for-config"})  # secret-check:allow (fake)
    _assert_installed(sandbox, result)
    config = _config(home)
    assert config["FACTS_PROVIDER"] == config["FACTS_PROVIDER_REQUESTED"] == "claude-code"
    assert config["FACTS_CLAUDE_BIN"] == str(bindir / "claude")  # absolute: launchd's PATH lacks it
    assert config["FACTS_MAX_EXCHANGES"] == "10"
    assert config["FACTS_SINCE"] == config["FACTS_INSTALLED_AT"]
    datetime.fromisoformat(config["FACTS_SINCE"])
    # The key in the shell is not written down, and the login check never saw it.
    assert "ANTHROPIC_API_KEY" not in config
    # …and was asked as the schedule will run it: nothing of this shell's environment.
    (seen,) = (sandbox[0] / "auth-env").read_text().splitlines()
    assert seen == f"key=none path={bindir}:/usr/bin:/bin:/usr/sbin:/sbin stub_dir=none"

    plist = plistlib.loads((home / "Library" / "LaunchAgents" / FACTS_PLIST).read_bytes())
    assert plist["ProgramArguments"] == [
        str(bindir / "uv"), "run", "--project", str(ROOT / "localhost" / "facts"), "python", "-m",
        "facts.single_machine", "--env-file", str(home / ".claude-chats" / "config.env")]
    assert plist["WorkingDirectory"] == str(ROOT)
    assert plist["StartInterval"] == 900 and not plist.get("RunAtLoad")
    assert f"launchctl bootstrap gui/{os.getuid()} {home / 'Library' / 'LaunchAgents' / FACTS_PLIST}" \
        in _calls(sandbox, "launchctl")
    # The hook and the drain know nothing of facts.
    assert "facts.single_machine" not in json.dumps(_settings(home))
    drain = plistlib.loads((home / "Library" / "LaunchAgents" /
                            "com.nakomis.claude-chats-drain.plist").read_bytes())
    assert "facts.single_machine" not in drain["ProgramArguments"]


def test_facts_timer_on_linux(sandbox):
    _, home, bindir = sandbox
    _assert_installed(sandbox, _run(sandbox, uname="Linux"))
    units = home / ".config" / "systemd" / "user"
    service = (units / "claude-chats-facts.service").read_text()
    assert f"WorkingDirectory={ROOT}" in service
    assert (f'ExecStart="{bindir / "uv"}" run --project "{ROOT / "localhost" / "facts"}" python -m '
            f'facts.single_machine --env-file "{home / ".claude-chats" / "config.env"}"') in service
    timer = (units / "claude-chats-facts.timer").read_text()
    assert "OnActiveSec=" in timer and "OnUnitActiveSec=15min" in timer
    assert "systemctl --user enable --now claude-chats-facts.timer" in _calls(sandbox, "systemctl")


def test_not_logged_in_turns_facts_off_and_a_later_run_turns_them_on(sandbox):
    _, home, _ = sandbox
    result = _run(sandbox, logged_in=False)
    _assert_installed(sandbox, result)  # the install itself goes ahead
    assert "Claude Facts is off" in result.stdout and "not logged in" in result.stdout
    config = _config(home)
    assert config["FACTS_PROVIDER"] == "off" and config["FACTS_PROVIDER_REQUESTED"] == "claude-code"
    assert "FACTS_SINCE" not in config
    assert not (home / "Library" / "LaunchAgents" / FACTS_PLIST).exists()

    _assert_installed(sandbox, _run(sandbox))  # logged in now
    config = _config(home)
    assert config["FACTS_PROVIDER"] == "claude-code" and config["FACTS_SINCE"]
    assert (home / "Library" / "LaunchAgents" / FACTS_PLIST).exists()


def test_facts_since_is_kept_on_re_runs(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox))
    first = _config(home)
    _assert_installed(sandbox, _run(sandbox, extra_env={"FACTS_SINCE": "all"}))  # ignored
    again = _config(home)
    assert again["FACTS_SINCE"] == first["FACTS_SINCE"]
    assert again["FACTS_INSTALLED_AT"] == first["FACTS_INSTALLED_AT"]


def test_anthropic_provider_needs_a_key_and_keeps_it_privately(sandbox):
    _, home, _ = sandbox
    result = _run(sandbox, extra_env={"FACTS_PROVIDER": "anthropic"})
    _assert_installed(sandbox, result)
    assert "needs ANTHROPIC_API_KEY" in result.stdout and _config(home)["FACTS_PROVIDER"] == "off"

    key = "sk-fake-facts-key"  # secret-check:allow (fake)
    _assert_installed(sandbox, _run(sandbox, extra_env={"FACTS_PROVIDER": "anthropic",
                                                        "ANTHROPIC_API_KEY": key}))
    _assert_installed(sandbox, _run(sandbox))  # remembered: provider and key
    config = _config(home)
    assert config["FACTS_PROVIDER"] == "anthropic" and config["ANTHROPIC_API_KEY"] == key
    add = [c for c in _calls(sandbox, "claude") if c.startswith("claude mcp add")][-1]
    assert key not in add  # the MCP server is not given it


def test_bedrock_facts_keep_the_aws_settings_with_another_embedder(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox, extra_env={
        "FACTS_PROVIDER": "bedrock", "AWS_PROFILE": "facts", "AWS_REGION": "eu-west-2"}))
    _assert_installed(sandbox, _run(sandbox))
    config = _config(home)
    assert config["CLAUDE_CHATS_PROVIDER"] == "ollama" and config["FACTS_PROVIDER"] == "bedrock"
    assert config["AWS_PROFILE"] == "facts" and config["AWS_REGION"] == "eu-west-2"


def test_facts_off_removes_the_schedule(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox))
    assert (home / "Library" / "LaunchAgents" / FACTS_PLIST).exists()
    _assert_installed(sandbox, _run(sandbox, extra_env={"FACTS_PROVIDER": "off"}))
    assert not (home / "Library" / "LaunchAgents" / FACTS_PLIST).exists()
    assert f"launchctl bootout gui/{os.getuid()}/com.nakomis.claude-chats-facts" in _calls(sandbox, "launchctl")
    _assert_installed(sandbox, _run(sandbox))  # stays off: that was a choice
    assert _config(home)["FACTS_PROVIDER"] == "off"


def test_an_unknown_facts_provider_stops_the_install(sandbox):
    result = _run(sandbox, extra_env={"FACTS_PROVIDER": "ollama"})
    assert result.returncode != 0 and "Unknown FACTS_PROVIDER" in result.stderr


def test_backfill_is_opt_in_and_can_be_stopped(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox))
    installed_at = _config(home)["FACTS_INSTALLED_AT"]
    mode = stat.S_IMODE((home / ".claude-chats" / "config.env").stat().st_mode)

    result = _run(sandbox, "--facts-backfill", "--since", "2026-01-01")
    assert result.returncode == 0, result.stderr
    assert _config(home)["FACTS_SINCE"] == "2026-01-01" and "10 exchanges per 15-minute run" in result.stdout
    assert _run(sandbox, "--facts-backfill").returncode == 0
    assert _config(home)["FACTS_SINCE"] == "all"
    _assert_installed(sandbox, _run(sandbox))  # a re-run keeps the backfill going
    assert _config(home)["FACTS_SINCE"] == "all"

    assert _run(sandbox, "--facts-backfill-stop").returncode == 0
    config = _config(home)
    assert config["FACTS_SINCE"] == config["FACTS_INSTALLED_AT"] == installed_at
    assert stat.S_IMODE((home / ".claude-chats" / "config.env").stat().st_mode) == mode == 0o600

    bad = _run(sandbox, "--facts-backfill", "--since", "last tuesday")
    assert bad.returncode != 0 and "is not a date" in bad.stderr
    assert _config(home)["FACTS_SINCE"] == installed_at


def test_backfill_refuses_when_facts_are_off(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox, extra_env={"FACTS_PROVIDER": "off"}))
    result = _run(sandbox, "--facts-backfill")
    assert result.returncode != 0 and "Claude Facts is off" in result.stderr


def test_facts_now_runs_the_same_command_as_the_schedule(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox))
    assert _run(sandbox, "--facts").returncode == 0
    assert (f"uv run --project {ROOT / 'localhost' / 'facts'} python -m facts.single_machine "
            f"--env-file {home / '.claude-chats' / 'config.env'}") in _calls(sandbox, "uv")


def test_uninstall_removes_the_facts_schedule(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox))
    assert _run(sandbox, "--uninstall").returncode == 0
    assert not (home / "Library" / "LaunchAgents" / FACTS_PLIST).exists()


def test_a_remembered_model_does_not_follow_a_change_of_provider(sandbox):
    _, home, _ = sandbox
    _assert_installed(sandbox, _run(sandbox, extra_env={
        "FACTS_PROVIDER": "bedrock", "AWS_PROFILE": "facts", "FACTS_MODEL": "eu.anthropic.claude-haiku-5-5"}))
    _assert_installed(sandbox, _run(sandbox))
    assert _config(home)["FACTS_MODEL"] == "eu.anthropic.claude-haiku-5-5"  # same provider: kept
    _assert_installed(sandbox, _run(sandbox, extra_env={"FACTS_PROVIDER": "claude-code"}))
    assert "FACTS_MODEL" not in _config(home)


def test_since_only_goes_with_a_backfill(sandbox):
    _assert_installed(sandbox, _run(sandbox))
    for args in (["--since", "2026-01-01"], ["--facts-backfill", "--since="]):
        result = _run(sandbox, *args)
        assert result.returncode != 0, args
