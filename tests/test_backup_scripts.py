"""scripts/backup.sh and restore.sh: which destination they pick, and pruning.

Run against stubs for docker and aws under a throwaway HOME. The default
matters more than it looks: installed com.nakomis.claude-chats-backup
LaunchAgents run backup.sh with no arguments, so with no flag it has to go on
uploading to S3 exactly as it always did. The single-machine edition passes
--local explicitly.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  ps) echo claude-chats-db ;;
  exec)
    if [[ "$*" == *pg_dump* ]]; then head -c 6000 /dev/urandom | base64
    elif [[ "$*" == *" -At "* ]]; then echo "id, conversation_id, message_uuid, role, author, content"
    else echo "row"; fi ;;
esac
exit 0
"""
AWS = """#!/bin/bash
echo "aws $*" >> "$STUB_LOG"
exit 0
"""


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", DOCKER), ("aws", AWS)):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return {
        "HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "calls.log"), "TMPDIR": str(tmp_path),
    }


def _backup(env, *args, **extra):
    return subprocess.run(["bash", str(ROOT / "scripts" / "backup.sh"), *args],
                          env={**env, **extra}, capture_output=True, text=True, timeout=60)


def _log(env):
    p = Path(env["STUB_LOG"])
    return p.read_text() if p.exists() else ""


def test_no_flag_still_uploads_to_s3(env):
    r = _backup(env)
    assert r.returncode == 0, r.stderr
    assert "aws s3 cp" in _log(env)
    assert not (Path(env["HOME"]) / ".claude-chats").exists()


def test_local_writes_to_home_and_never_touches_s3(env):
    r = _backup(env, "--local")
    assert r.returncode == 0, r.stderr
    dumps = list((Path(env["HOME"]) / ".claude-chats" / "backups").glob("claude_chats_*.sql.gz"))
    assert len(dumps) == 1
    assert "aws" not in _log(env)


def test_env_overrides_the_default(env):
    assert _backup(env, CLAUDE_CHATS_BACKUP_DEST="local").returncode == 0
    assert "aws" not in _log(env)


def test_a_bare_path_means_local(env, tmp_path):
    out = tmp_path / "dumps"
    assert _backup(env, str(out)).returncode == 0
    assert len(list(out.glob("claude_chats_*.sql.gz"))) == 1
    assert "aws" not in _log(env)


def test_local_prunes_to_the_newest_n(env, tmp_path):
    out = tmp_path / "dumps"
    out.mkdir()
    for day in range(1, 6):
        (out / f"claude_chats_202610{day:02d}T030000Z.sql.gz").write_bytes(b"old")
    (out / "keep-me.txt").write_text("not ours")
    r = _backup(env, "--local", str(out), CLAUDE_CHATS_BACKUP_KEEP="3")
    assert r.returncode == 0, r.stderr
    names = sorted(p.name for p in out.iterdir())
    assert names[:2] == ["claude_chats_20261004T030000Z.sql.gz", "claude_chats_20261005T030000Z.sql.gz"]
    assert len([n for n in names if n.startswith("claude_chats_")]) == 3
    assert "keep-me.txt" in names


def test_restore_defaults_to_s3_too(env):
    r = subprocess.run(["bash", str(ROOT / "scripts" / "restore.sh")], env=env,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode != 0  # the stub lists no backups
    assert "aws s3 ls" in _log(env)


def test_the_single_machine_installer_always_says_local():
    text = (ROOT / "localhost" / "install.sh").read_text()
    for script in ("backup.sh", "restore.sh"):
        calls = [line for line in text.splitlines() if f"scripts/{script}" in line]
        assert calls and all("--local" in line for line in calls), calls
