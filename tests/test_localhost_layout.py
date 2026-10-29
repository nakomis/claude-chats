"""Guard the single-machine edition against becoming a fork again (CHATS-36).

localhost/ was once a full copy of the hook, the MCP server and the schema.
Within a month the copies had fallen behind the originals — missing mid-turn
capture, author/tool/model capture, most of the schema — while a header still
claimed they were identical. So localhost/ now holds only what is genuinely
specific to running on one machine, and the code is the repository's own.
These tests keep it that way. Stdlib only.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALHOST = ROOT / "localhost"

CODE_SUFFIXES = {".py", ".sql"}


def test_no_python_or_sql_under_localhost():
    found = sorted(
        str(p.relative_to(ROOT)) for p in LOCALHOST.rglob("*")
        if p.is_file() and p.suffix in CODE_SUFFIXES
    )
    assert found == [], (
        "localhost/ must not carry code of its own; use ../hook, ../mcp and "
        f"../init.sql instead: {found}"
    )


def test_localhost_holds_only_install_files_and_docs():
    allowed_top = {"install.sh", "docker-compose.yml", "README.md", "INSTALL.md",
                   "launchd", "systemd", "docs", "hook"}
    assert {p.name for p in LOCALHOST.iterdir()} <= allowed_top


def test_localhost_hook_is_only_the_upgrade_shim():
    """localhost/hook is a pyproject with no code, pointing at ../../hook, for
    installs from before October 2026 until install.sh is re-run (CHATS-37).
    uv leaves a lock file and a .venv beside it; nothing else belongs there."""
    shim = LOCALHOST / "hook"
    assert {p.name for p in shim.iterdir()} - {"uv.lock", ".venv"} == {"pyproject.toml"}
    text = (shim / "pyproject.toml").read_text()
    assert 'claude-chats-hook = { path = "../../hook", editable = true }' in text
    assert "package = false" in text


def test_embed_py_exists_once():
    found = sorted(
        str(p.relative_to(ROOT)) for p in ROOT.rglob("embed.py")
        # .claude holds other worktrees when run from the main checkout.
        if not {".venv", ".git", ".claude", "node_modules"} & set(p.relative_to(ROOT).parts)
    )
    assert found == ["hook/hook/embed.py"]


def test_one_schema_and_compose_uses_it():
    compose = (LOCALHOST / "docker-compose.yml").read_text()
    assert "../init.sql:/docker-entrypoint-initdb.d/init.sql" in compose


def test_install_sh_uses_the_shared_packages():
    text = (LOCALHOST / "install.sh").read_text()
    assert 'HOOK_DIR="${ROOT_DIR}/hook"' in text
    assert 'MCP_DIR="${ROOT_DIR}/mcp"' in text
    assert "${ROOT_DIR}/init.sql" in text
    assert os.access(LOCALHOST / "install.sh", os.X_OK)


def test_nothing_imports_the_deleted_copies():
    """conversation_memory_mcp.embed and localhost/{hook,mcp,scripts} are gone."""
    stale = ("conversation_memory_mcp.embed", "conversation_memory_mcp import embed")
    hits = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if {".venv", ".git", ".claude", "node_modules"} & set(rel.parts):
            continue
        if rel == Path(__file__).relative_to(ROOT):
            continue
        text = p.read_text(errors="replace")
        hits += [f"{rel}: {s}" for s in stale if s in text]
    assert hits == []


def _timer_settings():
    settings = {}
    for line in (LOCALHOST / "systemd" / "claude-chats-drain.timer").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    return settings


def test_the_drain_timer_starts_whenever_it_is_enabled():
    """OnUnitActiveSec only repeats; something must fire first, measured from
    a moment that has not already passed when install.sh runs `enable --now`.
    OnBootSec alone fails that the moment the machine has been up a minute."""
    timer = _timer_settings()
    assert "OnUnitActiveSec" in timer
    starters = {"OnActiveSec", "OnCalendar"} & set(timer)
    assert starters, f"nothing starts the chain: {timer}"
    if starters == {"OnCalendar"}:
        assert timer.get("Persistent") == "true"
