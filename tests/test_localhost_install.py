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
        else:
            f.write_text(STUB.format(name=name, body=body))
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    # install.sh needs Python 3.11+, which /usr/bin/python3 may not be.
    (bindir / "python3").symlink_to(sys.executable)
    return tmp_path, home, bindir


def _run(sandbox, *args, uname="Darwin", extra_env=None):
    tmp_path, home, bindir = sandbox
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

    config = home / ".claude-chats" / "config.env"
    assert "CAPTURE_MODE" not in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    assert (sandbox[0] / "applied.sql").read_text() == (ROOT / "init.sql").read_text()
    assert any(f"-f {ROOT / 'localhost' / 'docker-compose.yml'} up -d" in c
               for c in _calls(sandbox, "docker"))
    assert f"uv sync --project {ROOT / 'hook'}" in _calls(sandbox, "uv")
    assert f"uv sync --project {ROOT / 'mcp'}" in _calls(sandbox, "uv")


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
                     if home not in p.parents and p.name not in ("calls.log", "applied.sql", "applies"))
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
