"""A real old single-machine install, upgraded in place by the new install.sh (CHATS-37).

tests/test_localhost_install.py checks what the new installer writes, starting
from a hand-made imitation of an old install. This goes further: it checks out
the single-machine edition as it was before it shared the main edition's code
(OLD_COMMIT, the last commit before #54), runs *that* install.sh, captures real
transcripts through the old hook it installed, and only then runs the new
install.sh over the same HOME and the same database. Then it checks that
nothing was lost and that everything now runs from the shared hook/ and mcp/.

The upgrade also adds Claude Facts (CHATS-39) with its default provider, the
user's Claude Code login: the stubbed `claude` says it is logged in and answers
`claude -p` as Claude Code does, and the facts schedule's own command is run to
show a fact extracted from a conversation after the upgrade, and none from
before it.

Each old capture mode is covered: `direct` (the hook wrote Postgres itself) and
`durable` (outbox plus a LaunchAgent), on macOS, and `durable` on Linux, where
the old installer scheduled no drain at all.

What is real and what is stubbed:

* Real: both install.sh scripts, uv, the old and new hook and drain processes,
  the MCP server (spoken to over stdio, as Claude Code would), psql, Postgres.
* Stubbed, on a PATH that comes first: claude (it keeps a fake MCP registry),
  launchctl, systemctl, plutil, ollama (the CLI; embeddings come from the fake
  Ollama in hook/tests/support.py), uname, and by default docker.
* HOME is a temporary directory, so ~/.claude, ~/.claude-chats and
  ~/Library/LaunchAgents are all throwaway.

Two ways to provide the database:

* Default: a scratch database on CLAUDE_CHATS_TEST_ADMIN_DSN (CI's pgvector
  service). The docker stub sends `docker exec -i claude-chats-db psql` there.
  Both installers hard-code the compose database's URL; the copies under test
  have that one line swapped for the scratch database's (asserted to be exactly
  one line), so a test run can never reach a real claude-chats-db on 5433.
* CLAUDE_CHATS_UPGRADE_DOCKER=1: real `docker compose`, real container, both
  installers unmodified. Run it through scripts/upgrade-check-localhost.sh,
  which refuses to start if a claude-chats-db container already exists and
  uses its own compose project, so a real install's volume is never touched.

The trees under test are copies: the old one from git, the new one from this
checkout (hook/, mcp/, init.sql, localhost/, scripts/), so that the uv
environments they create and the DB_URL edit stay out of the working tree.
"""

import json
import os
import plistlib
import queue
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hook" / "tests"))
from support import fake_ollama  # noqa: E402

# The last commit before #54 (CHATS-36): localhost/ with its own hook, MCP
# server, schema and capture modes. It stays reachable from main because this
# repository only allows merge commits (no squash, no rebase), so a full clone
# always has it.
OLD_COMMIT = "8b4d5cc96b79315959b8e718538e4a9d8adbab1e"
COMPOSE_DB_URL = "postgresql://claude:claude@localhost:5433/claude_chats"
DB_URL_LINE = f'DB_URL="{COMPOSE_DB_URL}"'

ADMIN_DSN = os.environ.get("CLAUDE_CHATS_TEST_ADMIN_DSN")
DOCKER = os.environ.get("CLAUDE_CHATS_UPGRADE_DOCKER") == "1"
PSQL = shutil.which("psql")
UV = shutil.which("uv")


def _old_commit_present() -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{OLD_COMMIT}^{{commit}}"],
                          capture_output=True).returncode == 0


def _why_not() -> str | None:
    if DOCKER:
        if not shutil.which("docker") or not UV:
            return "needs docker and uv on PATH"
    elif not ADMIN_DSN:
        return "CLAUDE_CHATS_TEST_ADMIN_DSN not set"
    elif not PSQL or not UV:
        return "needs psql and uv on PATH"
    if not _old_commit_present():
        # CI fetches full history for this; a shallow clone elsewhere skips.
        # (In CI a missing commit fails the module rather than skipping.)
        return f"old commit {OLD_COMMIT[:7]} not in this clone (shallow?)"
    return None


_skip = _why_not()
if _skip and (DOCKER or (os.environ.get("CI") and "DSN" not in _skip)):
    # Asked for by name, or in CI: a skip would let it quietly stop running.
    # (CI's own check already fails a job on a skip for want of a DSN.)
    raise RuntimeError(f"test_localhost_upgrade cannot run: {_skip}")
pytestmark = pytest.mark.skipif(bool(_skip), reason=_skip or "")


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

STUB = """#!/bin/bash
echo "{name} $*" >> "$STUB_LOG"
{body}
exit 0
"""

# `docker exec -i claude-chats-db psql … -U claude -d claude_chats` is the
# schema being applied: run the same psql against the scratch database.
DOCKER_STUB_BODY = r"""
if [[ "$1" == compose ]]; then exit 0; fi
if [[ "$1" == exec ]]; then
  shift
  [[ "$1" == -i ]] && shift
  shift  # the container name
  cmd="$1"; shift
  args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -U|-d) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  case "$cmd" in
    pg_isready) exit 0 ;;
    psql) exec "$PSQL_BIN" "$UPGRADE_DSN" "${args[@]}" ;;
  esac
fi
"""

# Claude Code's MCP registry, as far as `claude mcp add/remove` go; a logged-in
# `claude auth status`; and `claude -p`, answering with one fact as
# `--output-format json --json-schema` does.
CLAUDE_STUB = '''#!{python}
import json, os, sys
# The facts schedule runs this with launchd's near-empty environment, so it finds
# its own directory rather than relying on STUB_DIR.
stub_dir = os.environ.get("STUB_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.environ.get("STUB_LOG") or os.path.join(stub_dir, "calls.log"), "a") as log:
    log.write("claude " + " ".join(sys.argv[1:]) + "\\n")
path = os.path.join(stub_dir, "mcp-registry.json")
reg = json.load(open(path)) if os.path.exists(path) else {{}}
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai"}}))
    sys.exit(0)
if args[:1] == ["-p"]:
    sys.stdin.read()
    with open(os.path.join(stub_dir, "claude-p.log"), "a") as log:
        log.write(json.dumps({{"argv": args, "api_key": os.environ.get("ANTHROPIC_API_KEY")}}) + "\\n")
    fact = {{"id": "0", "text": "The lathe tooling notes are in lathe-tooling.md in the workshop folder.",
             "kind": "config", "subject": "lathe tooling", "attribute": "notes"}}
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "stop_reason": "tool_use",
                      "structured_output": {{"memory": [fact]}}, "result": "",
                      "usage": {{"input_tokens": 3, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 100, "output_tokens": 20}}}}))
    sys.exit(0)
if args[:2] == ["mcp", "remove"]:
    scope = args[args.index("--scope") + 1]
    if reg.pop(f"{{scope}}:{{args[2]}}", None) is None:
        sys.exit(1)
elif args[:2] == ["mcp", "add"]:
    sep = args.index("--")
    head, command = args[2:sep], args[sep + 1:]
    name, env, scope, i = head[0], {{}}, "local", 1
    while i < len(head):
        if head[i] == "--env":
            k, v = head[i + 1].split("=", 1); env[k] = v; i += 2
        elif head[i] == "--scope":
            scope = head[i + 1]; i += 2
        else:
            i += 1
    reg[f"{{scope}}:{{name}}"] = {{"command": command, "env": env}}
json.dump(reg, open(path, "w"), indent=1, sort_keys=True)
'''


@pytest.fixture
def world(tmp_path):
    """Temp HOME, stub PATH, a fake Ollama, and an empty database."""
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stubbed = ["launchctl", "systemctl", "plutil", "ollama", "uname"] + ([] if DOCKER else ["docker"])
    bodies = {
        "docker": DOCKER_STUB_BODY,
        "ollama": '[[ "$1" == list ]] && echo "mxbai-embed-large:latest  abc  669 MB"',
    }
    for name in stubbed:
        f = bindir / name
        if name == "uname":
            f.write_text('#!/bin/bash\necho "$FAKE_UNAME"\n')
        else:
            f.write_text(STUB.format(name=name, body=bodies.get(name, "")))
        f.chmod(0o755)
    (bindir / "claude").write_text(CLAUDE_STUB.format(python=sys.executable))
    (bindir / "claude").chmod(0o755)
    (bindir / "python3").symlink_to(sys.executable)

    path_dirs = [str(bindir), str(Path(UV).parent)]
    if DOCKER:
        path_dirs.append(str(Path(shutil.which("docker")).parent))
    path_dirs += ["/usr/bin", "/bin"]

    with _database() as (dsn, admin_dsn), fake_ollama() as (ollama_url, _):
        env = {
            "HOME": str(home),
            "PATH": ":".join(path_dirs),
            "TERM": "dumb",
            "STUB_LOG": str(tmp_path / "calls.log"),
            "STUB_DIR": str(tmp_path),
            "PSQL_BIN": PSQL or "",
            "UPGRADE_DSN": dsn,
            # Keep uv's caches and interpreter choice off the throwaway HOME,
            # or every run would download every wheel again.
            "UV_CACHE_DIR": subprocess.run([UV, "cache", "dir"], capture_output=True,
                                           text=True).stdout.strip(),
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_DOWNLOADS": "never",
            "PGOPTIONS": "--client-min-messages=warning",
        }
        if DOCKER:
            env.update(_docker_env())
        else:
            # A safety net, not configuration: every process the test starts
            # gets the scratch database through config.env, the registry or the
            # hook command, and these are all asserted on. Should one ever miss
            # it, it lands here rather than on the code's default, which is the
            # real claude-chats-db's port. (The environment wins over
            # config.env, and the two agree.)
            env["CLAUDE_CHATS_DB_URL"] = dsn
        # What the user had in their shell when they first installed. Only the
        # old installer sees it: everything after has to get it from what
        # that install recorded.
        provider = {"CLAUDE_CHATS_PROVIDER": "ollama", "OLLAMA_BASE_URL": ollama_url}
        w = World(tmp_path, home, env, dsn, admin_dsn, provider)
        try:
            yield w
        finally:
            w.settle()


def _docker_env() -> dict:
    """The Docker settings the installers' `docker` must share with this
    process's, so the guards below look at the daemon compose will use."""
    env = {k: os.environ[k] for k in ("DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
    # HOME is a throwaway; the CLI's plugins and contexts are in the real one.
    env["DOCKER_CONFIG"] = os.environ.get("DOCKER_CONFIG", os.path.expanduser("~/.docker"))
    env["COMPOSE_PROJECT_NAME"] = os.environ["COMPOSE_PROJECT_NAME"]
    return env


@contextmanager
def _database():
    """Yields (dsn, admin_dsn) of an empty database the installers will fill."""
    import psycopg
    from psycopg import sql

    if DOCKER:
        yield from _docker_database()
        return
    name = "claude_chats_upgrade_" + uuid.uuid4().hex[:12]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield _url(ADMIN_DSN, name), ADMIN_DSN
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))


def _docker_database():
    """The old installer's own container, under a compose project of our own.

    The container name is fixed (claude-chats-db) in both compose files, so
    one that already exists may be a real install: refuse rather than adopt
    it. The volume is named after COMPOSE_PROJECT_NAME, never localhost_pgdata.
    """
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if project in ("", "localhost"):
        pytest.fail("set COMPOSE_PROJECT_NAME to a throwaway name (see "
                    "scripts/upgrade-check-localhost.sh)")
    denv = {**os.environ, **_docker_env()}
    existing = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^claude-chats-db$"],
                              env=denv, capture_output=True, text=True, check=True).stdout.strip()
    if existing:
        pytest.fail("a claude-chats-db container already exists; refusing to touch it")
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family) as sock:
            sock.settimeout(1)
            if sock.connect_ex((addr, 5433)) == 0:
                pytest.fail(f"something is listening on {addr} port 5433; refusing")

    def remove():
        subprocess.run(["docker", "rm", "-f", "claude-chats-db"], env=denv, capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", f"{project}_pgdata"], env=denv,
                       capture_output=True)
        subprocess.run(["docker", "network", "rm", f"{project}_default"], env=denv,
                       capture_output=True)

    remove()  # a volume left by an aborted run of our own project, not anyone else's
    try:
        # The container does not exist until the old compose file starts it.
        yield COMPOSE_DB_URL, COMPOSE_DB_URL.rsplit("/", 1)[0] + "/postgres"
    finally:
        remove()


def _url(dsn: str, dbname: str) -> str:
    """``dsn`` pointed at ``dbname``, as a URL: the old direct-mode hook
    carries it unquoted on a shell command line, as it did the real one."""
    from urllib.parse import quote

    import psycopg
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    auth = quote(info.get("user", "postgres"))
    if info.get("password"):
        auth += ":" + quote(info["password"])
    host = info.get("host") or "localhost"
    return f"postgresql://{auth}@{host}:{info.get('port') or 5432}/{dbname}"


class World:
    def __init__(self, tmp, home, env, dsn, admin_dsn, provider):
        self.tmp, self.home, self.env, self.dsn, self.admin_dsn = tmp, home, env, dsn, admin_dsn
        self.provider = provider
        self.state = home / ".claude-chats"
        self.checkout = tmp / "claude-chats"

    # -- the two trees -----------------------------------------------------

    def old_tree(self) -> Path:
        """The checkout as it was: localhost/ from OLD_COMMIT."""
        self.checkout.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", OLD_COMMIT, "localhost"],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(self.checkout)], input=archive, check=True)
        self._point_at_database(self.checkout / "localhost" / "install.sh")
        return self.checkout / "localhost"

    def pull(self) -> Path:
        """`git pull` the same checkout up to this one. Like git, it deletes
        the old tracked files and leaves untracked ones, such as the old
        copies' .venv directories, where they were."""
        old_files = subprocess.run(
            ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", OLD_COMMIT, "localhost"],
            capture_output=True, text=True, check=True).stdout.split()
        for name in old_files:
            (self.checkout / name).unlink()
        ignore = shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", "*.pyc")
        for d in ("hook", "mcp", "localhost", "scripts", "facts", "facts_eval"):
            shutil.copytree(ROOT / d, self.checkout / d, ignore=ignore, dirs_exist_ok=True)
        shutil.copy2(ROOT / "init.sql", self.checkout / "init.sql")
        self._point_at_database(self.checkout / "localhost" / "install.sh")
        return self.checkout

    def start_old_container(self, old: Path):
        """Bring the old compose file's container up and wait until Postgres
        takes TCP connections, which it only does once the image's first-boot
        initialisation (the old init.sql included) is over. The old installer
        waits on `pg_isready` over the container's socket instead, which can
        answer during that initialisation; its own `up -d` is then a no-op."""
        import psycopg

        subprocess.run(["docker", "compose", "-f", str(old / "docker-compose.yml"), "up", "-d"],
                       env=self.env, check=True, capture_output=True)
        deadline, streak = time.monotonic() + 180, 0
        while streak < 2:
            assert time.monotonic() < deadline, "the old container never became ready"
            try:
                psycopg.connect(self.dsn, connect_timeout=3).close()
                streak += 1
            except psycopg.Error:
                streak = 0
            time.sleep(1)

    def settle(self, timeout: float = 60.0):
        """Wait for any drain the hook started in the background to finish,
        before its database and directory are removed underneath it."""
        import fcntl

        lock = self.state / "outbox.db.drain.lock"
        if not lock.exists():
            return
        deadline = time.monotonic() + timeout
        with open(lock, "a") as fh:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.2)

    def _point_at_database(self, script: Path):
        if DOCKER:
            return
        text = script.read_text()
        assert text.count(DB_URL_LINE) == 1, f"{script}: DB_URL line has changed shape"
        script.write_text(text.replace(DB_URL_LINE, f'DB_URL="{self.dsn}"'))

    # -- running things ----------------------------------------------------

    def install(self, script: Path, *args, uname="Darwin", check=True, old=False):
        env = {**self.env, **(self.provider if old else {}), "FAKE_UNAME": uname}
        r = subprocess.run(["bash", str(script), *args], env=env,
                           capture_output=True, text=True, timeout=600)
        if check:
            assert r.returncode == 0, f"{script} {args}\n{r.stdout}\n{r.stderr}"
        return r

    def hook_command(self) -> str:
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        cmds = {h["command"] for event in ("Stop", "UserPromptSubmit", "SessionEnd")
                for g in settings["hooks"][event] for h in g["hooks"]
                if "record-conversation" in h["command"]}
        assert len(cmds) == 1, cmds
        return cmds.pop()

    def capture(self, session_id: str, transcript: list[dict], check=True):
        """Run the installed hook exactly as Claude Code does: its command
        string through a shell, the hook payload on stdin."""
        path = self.tmp / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in transcript))
        payload = json.dumps({"session_id": session_id, "transcript_path": str(path),
                              "cwd": "/w/" + session_id, "git_branch": "main"})
        r = subprocess.run(["bash", "-c", self.hook_command()], input=payload, env=self.env,
                           capture_output=True, text=True, timeout=300)
        if check:
            assert r.returncode == 0, r.stderr
        return r

    def run_scheduled_drain(self, uname: str, check=True):
        """What launchd or systemd would run, read back from the installed unit."""
        if uname == "Darwin":
            plist = plistlib.loads(self.plist_path.read_bytes())
            argv, extra = plist["ProgramArguments"], plist.get("EnvironmentVariables", {})
        else:
            service = (self.systemd_dir / "claude-chats-drain.service").read_text()
            line = next(ln for ln in service.splitlines() if ln.startswith("ExecStart="))
            argv, extra = shlex.split(line[len("ExecStart="):]), {}
        # launchd and systemd pass next to nothing; the drain must find the
        # rest in config.env.
        r = subprocess.run(argv, env={**self.minimal_env(), **extra}, capture_output=True,
                           text=True, timeout=300)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r

    def run_scheduled_facts(self, uname: str):
        """The Claude Facts schedule's command, read back from the installed unit, run
        the way launchd or systemd would: from its working directory, with next to
        nothing in the environment."""
        if uname == "Darwin":
            plist = plistlib.loads(self.facts_plist_path.read_bytes())
            argv, cwd = plist["ProgramArguments"], plist["WorkingDirectory"]
        else:
            lines = (self.systemd_dir / "claude-chats-facts.service").read_text().splitlines()
            argv = shlex.split(next(ln for ln in lines if ln.startswith("ExecStart="))[len("ExecStart="):])
            cwd = next(ln for ln in lines if ln.startswith("WorkingDirectory="))[len("WorkingDirectory="):]
        r = subprocess.run(argv, cwd=cwd, env=self.minimal_env(), capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def minimal_env(self) -> dict:
        keep = ("HOME", "PATH", "TERM", "UV_CACHE_DIR", "UV_PYTHON", "UV_PYTHON_DOWNLOADS",
                "CLAUDE_CHATS_DB_URL")  # the last only ever the safety net above
        return {k: v for k, v in self.env.items() if k in keep}

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "com.nakomis.claude-chats-drain.plist"

    @property
    def facts_plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "com.nakomis.claude-chats-facts.plist"

    @property
    def systemd_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    def registry(self) -> dict:
        path = self.tmp / "mcp-registry.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def calls(self, prefix: str) -> list[str]:
        log = self.tmp / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [ln for ln in lines if ln.startswith(prefix + " ")]

    # -- looking at things -------------------------------------------------

    def sql(self, query: str, *params):
        import psycopg
        with psycopg.connect(self.dsn) as conn:
            return conn.execute(query, params).fetchall()

    def messages(self) -> dict:
        return {r[0]: r[1:] for r in self.sql(
            "SELECT m.message_uuid, c.session_id, m.role, m.content, m.created_at, "
            "       m.embedding IS NOT NULL "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id")}

    def outbox(self) -> dict:
        import sqlite3
        path = self.state / "outbox.db"
        if not path.exists():
            return {}
        with sqlite3.connect(path) as conn:
            return {r[0]: bool(r[1]) for r in conn.execute(
                "SELECT message_uuid, sent_at IS NOT NULL FROM outbox")}

    def wait_for(self, message_uuid: str, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sql("SELECT 1 FROM messages WHERE message_uuid = %s AND embedding IS NOT NULL",
                        message_uuid):
                return
            time.sleep(0.5)
        log = self.state / "drain.log"
        pytest.fail(f"{message_uuid} never reached Postgres; drain.log:\n"
                    + (log.read_text() if log.exists() else "(none)"))


def _schema(dsn: str) -> dict:
    """Everything init.sql defines that an upgrade could get wrong."""
    import psycopg
    with psycopg.connect(dsn) as conn:
        q = lambda s: sorted(conn.execute(s).fetchall())  # noqa: E731
        return {
            "columns": q("SELECT table_name, column_name, data_type, udt_name, is_nullable, "
                         "column_default FROM information_schema.columns "
                         "WHERE table_schema = 'public'"),
            "indexes": q("SELECT tablename, indexname, indexdef FROM pg_indexes "
                         "WHERE schemaname = 'public'"),
            "constraints": q("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                             "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"),
            "triggers": q("SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) "
                          "FROM pg_trigger WHERE NOT tgisinternal"),
            "functions": q("SELECT proname, pg_get_functiondef(oid) FROM pg_proc "
                           "WHERE pronamespace = 'public'::regnamespace AND prokind = 'f'"),
            "extensions": q("SELECT extname FROM pg_extension"),
        }


def _fresh_schema(world: World) -> dict:
    """The schema of a database built from nothing but the current init.sql."""
    import psycopg
    from psycopg import sql

    name = "claude_chats_fresh_" + uuid.uuid4().hex[:12]
    with psycopg.connect(world.admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info = psycopg.conninfo.conninfo_to_dict(world.admin_dsn)
    info["dbname"] = name
    dsn = psycopg.conninfo.make_conninfo(**info)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute((ROOT / "init.sql").read_text())
        return _schema(dsn)
    finally:
        with psycopg.connect(world.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)))


@contextmanager
def _database_closed(world: World):
    """Postgres refusing connections, as when Docker is not running."""
    import psycopg
    from psycopg import sql

    dbname = psycopg.conninfo.conninfo_to_dict(world.dsn)["dbname"]
    with psycopg.connect(world.admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
            sql.Identifier(dbname)))
        try:
            yield
        finally:
            admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                sql.Identifier(dbname)))


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

LONG_AGO = "2026-05-01T09:00:0{}+00:00"  # well outside search_memory's 3-month default


def _user(uid, text, ts):
    return {"type": "user", "uuid": uid, "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _assistant(uid, text, ts, tool_use=None):
    content = [{"type": "text", "text": text}]
    if tool_use:
        content.append({"type": "tool_use", "id": tool_use[0], "name": tool_use[1],
                        "input": {"command": "ls"}})
    return {"type": "assistant", "uuid": uid, "timestamp": ts,
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": content}}


def _old_session(tag: str) -> list[dict]:
    """A conversation from before the upgrade, dated months ago."""
    return [
        _user(f"{tag}-u1", f"Where did I leave the {tag} gearbox drawings?", LONG_AGO.format(1)),
        _assistant(f"{tag}-a1", f"The {tag} gearbox drawings are in the plan chest.",
                   LONG_AGO.format(2)),
        {"type": "custom-title", "customTitle": f"{tag} gearbox"},
    ]


def _new_session(tag: str) -> list[dict]:
    """A conversation after the upgrade: a tool call, and a message typed mid-turn."""
    now = datetime.now(timezone.utc)
    ts = lambda s: (now - timedelta(minutes=10) + timedelta(seconds=s)).isoformat()  # noqa: E731
    return [
        _user(f"{tag}-u1", "Please list the workshop folder of lathe tooling", ts(1)),
        _assistant(f"{tag}-a1", "Listing it now.", ts(2), tool_use=(f"{tag}-t1", "Bash")),
        {"type": "user", "uuid": f"{tag}-r1", "timestamp": ts(3),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": f"{tag}-t1",
              "content": "lathe-tooling.md\nchuck-keys.md"}]}},
        # HOME-411: typed while Claude was working, so it is an attachment.
        {"type": "attachment", "uuid": f"{tag}-q1", "timestamp": ts(4),
         "attachment": {"type": "queued_command", "prompt": "and the quick-change toolpost too",
                        "commandMode": "prompt", "origin": {"kind": "human"},
                        "timestamp": ts(4)}},
        _assistant(f"{tag}-a2", "The quick-change toolpost notes are in lathe-tooling.md.",
                   ts(5)),
        {"type": "custom-title", "customTitle": f"{tag} lathe tooling"},
    ]


# ---------------------------------------------------------------------------
# The MCP server, as Claude Code starts it
# ---------------------------------------------------------------------------

def _mcp(world: World, calls: list[tuple[str, dict]]) -> tuple[list[str], list[dict]]:
    """Start the registered conversation-memory server over stdio and make
    some tool calls. Returns (tool names, each call's result)."""
    reg = world.registry()["user:conversation-memory"]
    env = {k: v for k, v in world.minimal_env().items() if k != "CLAUDE_CHATS_DB_URL"}
    env.update(reg["env"])
    errlog = world.tmp / "mcp-stderr.log"
    with open(errlog, "w") as err:
        proc = subprocess.Popen(reg["command"], env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err, text=True)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(ln) for ln in proc.stdout] + [lines.put("")],
                     daemon=True).start()

    def send(msg):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        proc.stdin.flush()

    def reply(msg_id, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no reply to {msg_id}: " + errlog.read_text()) from None
            if not line:
                raise AssertionError("MCP server exited: " + errlog.read_text())
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                assert "error" not in msg, msg
                return msg["result"]

    try:
        send({"id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "upgrade-test", "version": "0"}}})
        reply(0)
        send({"method": "notifications/initialized"})
        send({"id": 1, "method": "tools/list"})
        tools = [t["name"] for t in reply(1)["tools"]]
        results = []
        for i, (name, args) in enumerate(calls, start=2):
            send({"id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
            res = reply(i)
            assert not res.get("isError"), res
            results.append(res.get("structuredContent") or json.loads(res["content"][0]["text"]))
        return tools, results
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def _snapshot(world: World, uname: str) -> dict:
    files = [world.home / ".claude" / "settings.json", world.state / "config.env"]
    files += ([world.plist_path, world.facts_plist_path] if uname == "Darwin" else
              [world.systemd_dir / f"claude-chats-{unit}.{kind}"
               for unit in ("drain", "facts") for kind in ("service", "timer")])
    return {
        "files": {str(p): p.read_bytes() for p in files},
        "registry": world.registry(),
        "schema": _schema(world.dsn),
        "messages": world.messages(),
        "outbox": world.outbox(),
    }


@pytest.mark.parametrize("old_mode,uname", [
    ("direct", "Darwin"),
    ("durable", "Darwin"),
    ("durable", "Linux"),
])
def test_an_old_install_upgrades_in_place(world, old_mode, uname):
    old = world.old_tree()
    if DOCKER:
        world.start_old_container(old)

    # ── The old install, used for a while ───────────────────────────────────
    # Everyone started durable or direct; a direct install that had ever been
    # durable has an outbox of sent rows too, so that history is built first.
    world.install(old / "install.sh", "--mode", "durable", uname=uname, old=True)
    world.capture("old-one", _old_session("first"))
    world.install(old / "install.sh", "--drain", uname=uname, old=True)
    if old_mode == "direct":
        world.install(old / "install.sh", "--mode", "direct", uname=uname, old=True)
        assert "CLAUDE_CHATS_CAPTURE_MODE=direct" in world.hook_command()
        world.capture("old-two", _old_session("second"))      # straight into Postgres
        with _database_closed(world):
            world.capture("old-lost", _old_session("third"))  # → the fallback file
    else:
        world.capture("old-two", _old_session("second"))      # pending in the outbox
        outbox = world.state / "outbox.db"
        outbox.chmod(0o444)
        try:
            world.capture("old-lost", _old_session("third"))  # → the fallback file
        finally:
            # SQLite gives a -shm it creates the database file's mode.
            for f in world.state.glob("outbox.db*"):
                f.chmod(0o644)

    before = world.messages()
    before_outbox = world.outbox()
    fallback = world.state / "outbox-fallback.ndjson"
    assert fallback.exists() and "third gearbox" in fallback.read_text()
    assert {"first-u1", "first-a1"} <= set(before)
    assert before_outbox["first-u1"] is True  # sent
    if old_mode == "direct":
        assert {"second-u1", "second-a1"} <= set(before)
    else:
        assert before_outbox["second-u1"] is False  # still pending
        assert "second-u1" not in before
    if uname == "Darwin" and old_mode == "durable":
        # The old agent, with the provider settings baked in.
        assert "EnvironmentVariables" in plistlib.loads(world.plist_path.read_bytes())
    else:
        assert not world.plist_path.exists()  # direct mode had no agent

    # ── The upgrade ──────────────────────────────────────────────────────────
    new = world.pull()
    assert (old / "hook" / ".venv").is_dir()  # what git leaves behind
    # Between the pull and the re-run, the installed hook still names
    # localhost/hook. Without the shim there it would exit 2, which Claude
    # Code takes as "block the prompt". With it, capture goes on, into the
    # outbox, whatever the old mode was.
    shim = new / "localhost" / "hook" / "pyproject.toml"
    shim.rename(shim.with_suffix(".away"))
    try:
        blocked = world.capture("gap", _old_session("gap"), check=False)
        assert blocked.returncode == 2, blocked.stderr  # the status Claude Code blocks on
    finally:
        shim.with_suffix(".away").rename(shim)
    world.capture("gap", _old_session("gap"))
    assert world.outbox().get("gap-u1") is False
    if uname == "Darwin" and old_mode == "durable":
        # The old LaunchAgent, still loaded, now runs the shared drain too.
        # The schema is not upgraded yet, so the write fails, and the rows
        # stay pending for the new schedule: late, never lost.
        result = world.run_scheduled_drain(uname, check=False)
        assert result.returncode != 0 and 'column "author"' in result.stderr
        assert world.outbox()["gap-u1"] is False
    result = world.install(new / "localhost" / "install.sh", uname=uname)
    if old_mode == "direct":
        assert "direct capture mode, which has been removed" in result.stdout
    assert "--replay-fallback" in result.stdout

    # Every row that was in Postgres is still there, unchanged.
    after = world.messages()
    assert {k: after.get(k) for k in before} == before

    # The schema is exactly what the current init.sql builds from nothing.
    schema = _schema(world.dsn)
    assert schema == _fresh_schema(world)
    assert any(name == "messages_created_at_brin" and "USING brin (created_at)" in d
               for _, name, d in schema["indexes"])  # HOME-464
    tables = {t for t, *_ in schema["columns"]}
    assert {"facts", "fact_events", "facts_progress"} <= tables
    columns = {c for t, c, *_ in schema["columns"] if t == "messages"}
    assert {"author", "tool_name", "model", "embedder"} <= columns  # CHATS-5 / HOME-311

    # Capture now runs from the shared hook/, through the outbox.
    hook = shlex.split(world.hook_command())
    assert hook == ["CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1", "uv", "run", "--project",
                    str(new / "hook"), "record-conversation"]
    settings = (world.home / ".claude" / "settings.json").read_text()
    assert "localhost/hook" not in settings and "CAPTURE_MODE" not in settings

    # The MCP server is the shared mcp/, and nothing else is registered.
    reg = world.registry()
    assert list(reg) == ["user:conversation-memory"]
    assert reg["user:conversation-memory"]["command"] == [
        "uv", "run", "--project", str(new / "mcp"), "conversation-memory-mcp"]
    assert "CLAUDE_CHATS_READ_DB_URL" not in reg["user:conversation-memory"]["env"]

    # config.env: no capture mode, the provider carried over, private.
    config = world.state / "config.env"
    keys = [ln.split("=", 1)[0] for ln in config.read_text().splitlines()
            if ln and not ln.startswith("#")]
    assert keys == ["CLAUDE_CHATS_PROVIDER", "CLAUDE_CHATS_MODEL", "CLAUDE_CHATS_DIMENSIONS",
                    "CLAUDE_CHATS_DB_URL", "OLLAMA_BASE_URL",
                    "FACTS_PROVIDER", "FACTS_PROVIDER_REQUESTED", "FACTS_INSTALLED_AT", "FACTS_SINCE",
                    "FACTS_MAX_EXCHANGES", "FACTS_CLAUDE_BIN"]
    # Claude Facts was added with its default provider, for conversations from now on.
    facts_config = dict(ln.split("=", 1) for ln in config.read_text().splitlines()
                        if ln.startswith("FACTS_"))
    assert facts_config["FACTS_PROVIDER"] == "claude-code"
    assert facts_config["FACTS_CLAUDE_BIN"] == str(world.tmp / "bin" / "claude")
    facts_since = datetime.fromisoformat(facts_config["FACTS_SINCE"])
    assert facts_config["FACTS_INSTALLED_AT"] == facts_config["FACTS_SINCE"]
    assert abs(datetime.now(timezone.utc) - facts_since) < timedelta(minutes=15)
    # Carried over from the old install's config.env: this run's environment
    # had none of it.
    assert f"OLLAMA_BASE_URL={world.provider['OLLAMA_BASE_URL']}" in config.read_text()
    assert f"CLAUDE_CHATS_DB_URL={world.dsn}" in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    # The new drain schedule, pointing at the shared hook/ and the config file.
    drain_argv_tail = ["run", "--project", str(new / "hook"), "drain-outbox",
                       "--env-file", str(config)]
    if uname == "Darwin":
        plist = plistlib.loads(world.plist_path.read_bytes())
        assert plist["ProgramArguments"][1:] == drain_argv_tail
        # The old plist had the provider settings (an API key, perhaps) baked in.
        assert "EnvironmentVariables" not in plist
        launchctl = world.calls("launchctl")
        assert launchctl[-4].startswith("launchctl bootout gui/")
        assert launchctl[-3] == f"launchctl bootstrap gui/{os.getuid()} {world.plist_path}"
        assert launchctl[-1] == f"launchctl bootstrap gui/{os.getuid()} {world.facts_plist_path}"
        facts_plist = plistlib.loads(world.facts_plist_path.read_bytes())
        assert facts_plist["ProgramArguments"][1:] == [
            "run", "--project", str(new / "localhost" / "facts"), "python", "-m", "facts.single_machine",
            "--env-file", str(config)]
        assert facts_plist["WorkingDirectory"] == str(new)
    else:
        service = (world.systemd_dir / "claude-chats-drain.service").read_text()
        assert f'--project "{new / "hook"}" drain-outbox --env-file "{config}"' in service
        assert (world.systemd_dir / "claude-chats-drain.timer").exists()
        assert world.calls("systemctl")[-1] == \
            "systemctl --user enable --now claude-chats-facts.timer"
        assert "systemctl --user enable --now claude-chats-drain.timer" in world.calls("systemctl")
        facts_unit = (world.systemd_dir / "claude-chats-facts.service").read_text()
        assert f"WorkingDirectory={new}" in facts_unit
        assert f'--project "{new / "localhost" / "facts"}" python -m facts.single_machine' in facts_unit
        assert not (world.home / "Library").exists()

    # Running it again changes nothing at all.
    snap = _snapshot(world, uname)
    world.install(new / "localhost" / "install.sh", uname=uname)
    assert _snapshot(world, uname) == snap
    assert not list(world.state.glob("*.tmp*"))

    # ── After the upgrade ────────────────────────────────────────────────────
    # What the old install left pending is delivered by the new schedule
    # (launchd runs it at load; systemd a minute after enabling).
    world.run_scheduled_drain(uname)
    assert all(world.outbox().values()), world.outbox()
    for uid in ("second-u1", "second-a1", "gap-u1", "gap-a1"):
        assert uid in world.messages()
    # …and the fallback file by the replay the installer pointed out.
    world.install(new / "localhost" / "install.sh", "--replay-fallback", uname=uname)
    assert {"third-u1", "third-a1"} <= set(world.messages())

    # A new conversation, through the new hook and the drain it starts itself.
    world.capture("new-one", _new_session("fresh"))
    world.wait_for("fresh-a2")
    rows = {r[0]: r[1:] for r in world.sql(
        "SELECT message_uuid, author, tool_name, model, embedding IS NOT NULL FROM messages "
        "WHERE message_uuid LIKE 'fresh-%%'")}
    assert rows["fresh-u1"] == ("martin", None, None, True)
    assert rows["fresh-a1"] == ("claude", None, "claude-opus-5-5", True)
    assert rows["fresh-r1"] == ("tool", "Bash", None, True)
    assert rows["fresh-q1"] == ("martin", None, None, True)  # HOME-411: mid-turn
    # Rows captured before the upgrade keep the honest 'unknown'.
    assert world.sql("SELECT author FROM messages WHERE message_uuid = 'first-u1'") == \
        [("unknown",)]

    # A session from before the upgrade, resumed after it: the new hook sees
    # the whole transcript again. Nothing is duplicated or re-dated, and the
    # new message lands in the same conversation.
    resumed = _old_session("second") + [
        _user("second-u2", "And the gearbox bearings?", datetime.now(timezone.utc).isoformat())]
    world.capture("old-two", resumed)
    world.wait_for("second-u2")
    convs = world.sql("SELECT c.id, count(*) FROM conversations c "
                      "JOIN messages m ON m.conversation_id = c.id "
                      "WHERE c.session_id = 'old-two' GROUP BY c.id")
    assert [n for _, n in convs] == [3]
    now = world.messages()
    if old_mode == "direct":  # in Postgres since before the upgrade
        for uid in ("second-u1", "second-a1"):
            assert now[uid][:4] == after[uid][:4]  # session, role, content, date
    assert now["second-u1"][3] == datetime.fromisoformat(LONG_AGO.format(1))

    # Claude Facts, as its schedule runs it: a conversation from after the upgrade
    # gives a fact, embedded as queries are; nothing from before FACTS_SINCE is read.
    now = datetime.now(timezone.utc)
    world.capture("facts-one", [
        _user("facts-u1", "Where are the lathe tooling notes?", (now + timedelta(seconds=1)).isoformat()),
        _assistant("facts-a1", "In lathe-tooling.md in the workshop folder.", (now + timedelta(seconds=2)).isoformat()),
        _user("facts-u2", "Thanks, and the chuck keys?", (now + timedelta(seconds=3)).isoformat()),
    ])
    world.wait_for("facts-u2")
    world.run_scheduled_facts(uname)
    facts = world.sql("SELECT c.session_id, f.embedder, f.embedding IS NOT NULL FROM facts f "
                      "JOIN conversations c ON c.id = f.conversation_id")
    assert ("facts-one", "ollama-mxbai-embed-large", True) in facts, facts
    assert not any(sid.startswith(("old-", "gap")) for sid, *_ in facts), facts
    calls = [json.loads(ln) for ln in (world.tmp / "claude-p.log").read_text().splitlines()]
    assert calls and all(c["api_key"] is None and "--bare" not in c["argv"] for c in calls)

    # And the MCP server Claude Code would start finds all of it.
    tools, (recent, everything, old_only) = _mcp(world, [
        ("search_memory", {"query": "quick-change toolpost lathe tooling"}),
        ("search_memory", {"query": "first gearbox drawings plan chest", "since": "all"}),
        ("search_memory", {"query": "first gearbox drawings plan chest"}),
    ])
    assert {"search_memory", "get_conversation", "list_recent_sessions",
            "recall_facts"} <= set(tools)
    assert recent["results"][0]["session_id"] == "new-one"
    # CHATS-14: the default window is 3 months, and since="all" lifts it.
    assert recent["since"] == "3 months"
    assert everything["results"][0]["session_id"] == "old-one"
    assert all(r["session_id"] != "old-one" for r in old_only["results"])
    # CHATS-15: the vector half ran (so did _set_probes), and the probes it
    # read back were the ones it set; anything else would be a warning. (A
    # pgvector older than 0.8 adds one about iterative scans, which is fine.)
    for out in (recent, everything, old_only):
        assert out["mode_used"] == "hybrid", out
        assert "ivfflat.probes" not in (out.get("warning") or ""), out
