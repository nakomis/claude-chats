"""The monthly-archive backup (CHATS-41): convmem-archive.sh, backup.sh, restore.sh.

Round trips against a real Postgres with pgvector, through `docker exec` the
way the scripts run everywhere: seed messages across several months plus
Claude Facts, archive, take the nightly, wipe, restore, and compare every
table row for row. Both targets: a local directory (the single-machine
edition) and S3 laid out exactly as Luke writes it, the S3 side a directory
behind tests/fixtures/aws-s3-stub.

The database is reached one of two ways:

  CONVMEM_TEST_CONTAINER   a running Postgres container (CI: the pgvector
                           service container); the scripts `docker exec`
                           into it for real.
  CONVMEM_TEST_PG_BIN      a directory of psql/pg_dump matching a server on
                           CLAUDE_CHATS_TEST_ADMIN_DSN's host and port; `docker`
                           is tests/fixtures/docker-pg-stub. For a machine
                           without Docker.

Either way CLAUDE_CHATS_TEST_ADMIN_DSN must reach the same server, to seed it.
Skipped without one of them; CI fails the job if that happens.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
ADMIN_DSN = os.environ.get("CLAUDE_CHATS_TEST_ADMIN_DSN")
CONTAINER = os.environ.get("CONVMEM_TEST_CONTAINER")
PG_BIN = os.environ.get("CONVMEM_TEST_PG_BIN")

needs_db = pytest.mark.skipif(
    not ADMIN_DSN or not (CONTAINER or PG_BIN),
    reason="CLAUDE_CHATS_TEST_ADMIN_DSN with CONVMEM_TEST_CONTAINER or CONVMEM_TEST_PG_BIN not set")

# Primary keys, for comparing tables row for row.
TABLES = {"conversations": "id", "messages": "id", "facts": "id",
          "fact_events": "id", "facts_progress": "conversation_id"}

SEED = """
INSERT INTO conversations (session_id, host) SELECT 's' || g, 'test' FROM generate_series(1, 4) g;
-- Five messages a month for the current month and the three before it. Real
-- 1024-float vectors, so they round-trip too and the size floors see
-- realistic bytes per row.
INSERT INTO messages (conversation_id, message_uuid, role, author, content, sequence_num,
                      created_at, embedding, embedder)
SELECT c.id, c.session_id || '-' || g, 'user', 'martin', 'message ' || g || ' ' || md5(c.session_id || g),
       g, date_trunc('month', now()) - make_interval(months => substr(c.session_id, 2)::int - 1)
          + interval '2 days' + g * interval '1 minute',
       (SELECT array_agg(random())::vector(1024) FROM generate_series(1, 1024) x WHERE g > 0),
       'ollama-mxbai-q8'
FROM conversations c, generate_series(1, 5) g;
-- Claude Facts: one per conversation, one superseding another (a reference
-- within the table), one with no conversation, events and progress.
INSERT INTO facts (text, kind, subject, attribute, observed_at, last_confirmed, conversation_id,
                   extractor, embedding)
SELECT 'fact ' || session_id, 'config', 'Mace', 'RAM', now(), now(), id, 'test',
       (SELECT array_agg(random())::vector(1024) FROM generate_series(1, 1024) x WHERE session_id <> '')
FROM conversations;
INSERT INTO facts (text, kind, observed_at, last_confirmed, source, extractor)
VALUES ('from a playbook', 'identity', now(), now(), 'luke/ansible/playbook.yml', 'test');
UPDATE facts SET superseded_by = (SELECT id FROM facts WHERE text = 'fact s4'), valid_to = now()
WHERE text = 'fact s1';
INSERT INTO fact_events (fact_id, action, reason, actor) SELECT id, 'confirmed', 'seed', 'test' FROM facts;
INSERT INTO facts_progress (conversation_id, last_seq) SELECT id, 3 FROM conversations;
"""


def _months_ago(conn, n):
    return conn.execute(
        "SELECT to_char(date_trunc('month', now()) - make_interval(months => %s), 'YYYY-MM')",
        (n,)).fetchone()[0]


@pytest.fixture
def db(tmp_path):
    """A seeded database, and the environment that points the scripts at it."""
    import psycopg
    from psycopg import sql

    name = "convmem_backup_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    # The scripts connect as claude, as they do in claude-chats-db, where the
    # image makes POSTGRES_USER a superuser.
    if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'claude'").fetchone():
        admin.execute("CREATE ROLE claude SUPERUSER LOGIN PASSWORD 'claude'")
    admin.execute(sql.SQL("CREATE DATABASE {} OWNER claude").format(sql.Identifier(name)))
    info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    info["dbname"] = name
    dsn = psycopg.conninfo.make_conninfo(**info)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute((ROOT / "init.sql").read_text())
        conn.execute(SEED)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    shutil.copy(FIXTURES / "aws-s3-stub", bindir / "aws")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home), "TMPDIR": str(tmp_path), "LANG": "C",
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CONVMEM_DB_NAME": name,
        # The S3 target stages under Luke's /mnt/data by default.
        "CONVMEM_STAGING_DIR": str(tmp_path / "staging"),
        "STUB_S3_ROOT": str(tmp_path / "s3"), "STUB_AWS_LOG": str(tmp_path / "aws.log"),
        # Never a real notification from a test.
        "CLAUDE_CHATS_BACKUP_NOTIFY": "0",
    }
    if CONTAINER:
        env["CONVMEM_CONTAINER"] = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}", CONTAINER],
            capture_output=True, text=True, check=True).stdout.strip().lstrip("/")
    else:
        shutil.copy(FIXTURES / "docker-pg-stub", bindir / "docker")
        env.update(STUB_PG_HOST=info.get("host", "localhost"), STUB_PG_PORT=str(info.get("port", 5432)),
                   STUB_PG_BIN=PG_BIN)
    try:
        yield dsn, env, tmp_path
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _run(env, script, *args, **extra):
    r = subprocess.run([str(SCRIPTS / script), *args], env={**env, **extra},
                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    return r


def _ok(r):
    assert r.returncode == 0, f"exit {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return r.stdout.strip()


def _snapshot(dsn):
    """Every table's rows, as one checksum per table, plus the counts."""
    import psycopg
    with psycopg.connect(dsn) as conn:
        out = {}
        for table, key in TABLES.items():
            out[table] = conn.execute(
                f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY {key}), '')) FROM {table} t"
            ).fetchone()
        return out


def _gzip_to(path, data):
    import gzip
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)


# ── local target ─────────────────────────────────────────────────────────────

@needs_db
def test_local_round_trip(db):
    dsn, env, tmp = db
    backups = tmp / "backups"
    before = _snapshot(dsn)
    assert before["messages"][0] == 20 and before["facts"][0] == 5

    _ok(_run(env, "backup.sh", str(backups)))

    months = sorted(p.name for p in (backups / "archive" / "messages").iterdir())
    assert len(months) == 3 and all(".r5." in m for m in months)
    nightlies = list((backups / "nightly").glob("claude_chats_*.sql.gz"))
    assert len(nightlies) == 1

    out = _ok(_run(env, "restore.sh", "--local", str(backups), "--yes", "--no-backfill"))
    assert "3 archived month(s)" in out
    assert _snapshot(dsn) == before

    # The identity sequence came back too: a new event does not collide.
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO fact_events (fact_id, action, actor) "
                     "SELECT id, 'restored', 'test' FROM facts LIMIT 1")


@needs_db
def test_local_nightlies_are_pruned_but_months_never_are(db):
    dsn, env, tmp = db
    backups = tmp / "backups"
    nightly = backups / "nightly"
    nightly.mkdir(parents=True)
    for day in range(1, 6):
        (nightly / f"claude_chats_202610{day:02d}T030000Z.sql.gz").write_bytes(b"old")
    (nightly / "keep-me.txt").write_text("not ours")
    _ok(_run(env, "backup.sh", str(backups), CLAUDE_CHATS_BACKUP_KEEP="2"))
    names = sorted(p.name for p in nightly.iterdir())
    assert names[0] == "claude_chats_20261005T030000Z.sql.gz"
    assert len([n for n in names if n.startswith("claude_chats_")]) == 2
    assert "keep-me.txt" in names
    assert len(list((backups / "archive" / "messages").iterdir())) == 3


@needs_db
def test_a_changed_month_is_recut_and_replaces_its_file(db):
    import psycopg
    dsn, env, tmp = db
    backups = tmp / "backups"
    _ok(_run(env, "backup.sh", str(backups)))
    archive = backups / "archive" / "messages"
    with psycopg.connect(dsn, autocommit=True) as conn:
        month = _months_ago(conn, 2)
        current = _months_ago(conn, 0)
        old = [p.name for p in archive.iterdir() if p.name.startswith(f"messages_{month}.")]
        # A backfill that rewrites text in an archived month.
        conn.execute("UPDATE messages SET content = content || ' (edited)' "
                     "WHERE to_char(created_at, 'YYYY-MM') = %s AND sequence_num = 1", (month,))

    local = {**env, "CONVMEM_TARGET": "local", "CONVMEM_ARCHIVE_DIR": str(backups / "archive")}
    # The month no longer matches its key, so the nightly has to carry it again.
    assert _ok(_run(local, "convmem-archive.sh", "cutoff")) == f"{month}-01"

    _ok(_run(env, "backup.sh", str(backups)))
    new = [p.name for p in archive.iterdir() if p.name.startswith(f"messages_{month}.")]
    assert len(new) == 1 and new != old
    assert _ok(_run(local, "convmem-archive.sh", "cutoff")) == f"{current}-01"

    after = _snapshot(dsn)
    _ok(_run(env, "restore.sh", str(backups), "--yes", "--no-backfill"))
    assert _snapshot(dsn) == after


@needs_db
def test_restore_refuses_a_truncated_month(db):
    dsn, env, tmp = db
    backups = tmp / "backups"
    _ok(_run(env, "backup.sh", str(backups)))
    victim = sorted((backups / "archive" / "messages").iterdir())[0]
    victim.write_bytes(victim.read_bytes()[:1000])
    r = _run(env, "restore.sh", str(backups), "--yes", "--no-backfill")
    assert r.returncode != 0
    assert "truncated" in r.stderr


@needs_db
def test_a_pre_chats_41_whole_dump_still_restores(db):
    """Old backup.sh wrote whole-database dumps straight into the directory."""
    dsn, env, tmp = db
    before = _snapshot(dsn)
    backups = tmp / "old-backups"
    dump = subprocess.run(
        ["bash", "-c", f'docker exec "$CONVMEM_CONTAINER" pg_dump -h /var/run/postgresql -U claude "{env["CONVMEM_DB_NAME"]}"'],
        env={**env, "CONVMEM_CONTAINER": env.get("CONVMEM_CONTAINER", "claude-chats-db")},
        capture_output=True, check=True).stdout
    _gzip_to(backups / "claude_chats_20261001T030000Z.sql.gz", dump)
    out = _ok(_run(env, "restore.sh", str(backups), "--yes", "--no-backfill"))
    assert "whole-database dump" in out
    assert _snapshot(dsn) == before


# ── S3 target, as Luke runs it ───────────────────────────────────────────────

def _luke_nightly(env, tmp):
    """What home-infra's backup.sh does with `dump`: gzip it and stream it up."""
    key = "luke_claude_chats_20261029T023000Z.sql.gz"
    prefix = env.get("BACKUP_PREFIX", "")
    subprocess.run(
        ["bash", "-o", "pipefail", "-c",
         f'"{SCRIPTS}/convmem-archive.sh" dump | gzip | AWS_PROFILE=taiga-backup aws s3 cp - '
         f'"s3://nakomis-taiga-backups/{prefix}conversation-memory/{key}" --region eu-west-2'],
        env=env, check=True, capture_output=True)
    return key


@needs_db
@pytest.mark.parametrize("prefix", ["", "dr/"])
def test_s3_round_trip_in_lukes_layout(db, prefix):
    dsn, env, tmp = db
    env = {**env, "BACKUP_PREFIX": prefix}
    before = _snapshot(dsn)

    out = _run(env, "convmem-archive.sh", "archive")
    _ok(out)
    archived = sorted((tmp / "s3" / "nakomis-convmem-archive" / prefix / "messages").iterdir())
    assert len(archived) == 3
    for obj in archived:
        # messages_YYYY-MM.r<rows>.c<content>.e<hash>.a<hash>.b<bytes>.csv.gz, and b is the size.
        parts = obj.name.split(".")
        assert parts[1] == "r5" and parts[2].startswith("c") and parts[5] == f"b{obj.stat().st_size}"
    log = (tmp / "aws.log").read_text()
    uploads = [line for line in log.splitlines() if " s3 cp " in line]
    assert all(line.startswith("AWS_PROFILE=taiga-backup ") for line in uploads)
    assert all("--storage-class GLACIER_IR" in line and "--region eu-west-2" in line for line in uploads)
    assert all("--metadata sha256=" in line for line in uploads)

    _luke_nightly(env, tmp)

    out = _ok(_run(env, "restore.sh", "--s3", "--yes", "--no-backfill"))
    assert "3 archived month(s)" in out
    assert _snapshot(dsn) == before
    reads = [line for line in (tmp / "aws.log").read_text().splitlines()[len(log.splitlines()):]
             if "aws s3" in line and "taiga-backup" not in line]
    assert reads and all(line.startswith("AWS_PROFILE=nakom.is-admin ") for line in reads)


@needs_db
def test_a_failed_archive_never_leaves_a_month_in_neither_place(db):
    """HOME-458's guarantee: the cutoff comes from the archive, not the calendar."""
    import psycopg
    dsn, env, tmp = db
    with psycopg.connect(dsn) as conn:
        oldest, current = _months_ago(conn, 3), _months_ago(conn, 0)
    before = _snapshot(dsn)

    failing = {**env, "STUB_AWS_FAIL_CP": "s3://nakomis-convmem-archive/"}
    r = _run(failing, "convmem-archive.sh", "archive")
    assert r.returncode != 0
    assert "upload failed" in r.stderr
    assert not (tmp / "s3" / "nakomis-convmem-archive").exists()

    # Nothing archived: the cutoff stays at the oldest month, and the nightly
    # carries every message instead.
    assert _ok(_run(env, "convmem-archive.sh", "cutoff")) == f"{oldest}-01"
    assert _ok(_run(env, "convmem-archive.sh", "expect")) == "20"
    _luke_nightly(env, tmp)

    # That nightly alone restores everything.
    _ok(_run(env, "restore.sh", "--s3", "--yes", "--no-backfill"))
    assert _snapshot(dsn) == before

    # Once archiving works again, the cutoff advances by itself.
    _ok(_run(env, "convmem-archive.sh", "archive"))
    assert _ok(_run(env, "convmem-archive.sh", "cutoff")) == f"{current}-01"
    assert _ok(_run(env, "convmem-archive.sh", "expect")) == "5"


@needs_db
def test_the_local_wrapper_survives_a_failed_archive(db):
    """backup.sh warns and takes a bigger nightly rather than none."""
    dsn, env, tmp = db
    backups = tmp / "backups"
    (backups / "archive").mkdir(parents=True)
    (backups / "archive" / "messages").write_text("not a directory")  # archiving cannot write
    before = _snapshot(dsn)
    r = _run(env, "backup.sh", str(backups))
    _ok(r)
    assert "archiving old months failed" in r.stderr
    (backups / "archive" / "messages").unlink()
    _ok(_run(env, "restore.sh", str(backups), "--yes", "--no-backfill"))
    assert _snapshot(dsn) == before


# ── No database needed ───────────────────────────────────────────────────────

@pytest.fixture
def nodb(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/bin/sh\necho \"docker $*\" >> \"$STUB_LOG\"\nexit 1\n")
    docker.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin", "STUB_LOG": str(tmp_path / "calls.log"),
            "TMPDIR": str(tmp_path), "CLAUDE_CHATS_BACKUP_NOTIFY": "0"}


def test_the_retired_s3_flag_is_refused(nodb):
    r = _run(nodb, "backup.sh", "--s3")
    assert r.returncode == 3
    assert "retired" in r.stderr and "Luke" in r.stderr
    assert not Path(nodb["STUB_LOG"]).exists()


def test_a_machine_still_carrying_the_old_agent_is_told_to_remove_it(nodb):
    agents = Path(nodb["HOME"]) / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.nakomis.claude-chats-backup.plist").write_text("<plist/>")
    r = _run(nodb, "backup.sh")
    assert r.returncode == 3
    assert "launchctl bootout" in r.stderr
    assert not Path(nodb["STUB_LOG"]).exists()


def test_a_stopped_container_fails_the_backup(nodb):
    r = _run(nodb, "backup.sh")
    assert r.returncode != 0
    assert not list((Path(nodb["HOME"]) / ".claude-chats" / "backups" / "nightly").glob("*.sql.gz"))


def test_an_unknown_target_is_refused(nodb):
    r = _run(nodb, "convmem-archive.sh", "cutoff", CONVMEM_TARGET="ftp")
    assert r.returncode == 2


def test_the_old_mac_backup_files_are_gone():
    assert not (SCRIPTS / "setup-backup.sh").exists()
    assert not (SCRIPTS / "com.nakomis.claude-chats-backup.plist").exists()


# home-infra vendors scripts/convmem-archive.sh byte for byte as
# scripts/luke/convmem-archive.sh, and its scripts/check-convmem-archive.sh pins
# this same value. Change the script and both go red until the vendored copy and
# both pins are updated together (the init.sql pattern, HOME-194).
ARCHIVER_SHA256 = "11c21d83152b3dc3c10011eedf3ecebb3f0b492c400d94765d28663bcced768f"


def test_the_archiver_matches_the_checksum_home_infra_pins():
    import hashlib
    actual = hashlib.sha256((SCRIPTS / "convmem-archive.sh").read_bytes()).hexdigest()
    assert actual == ARCHIVER_SHA256, (
        f"scripts/convmem-archive.sh is now {actual}. If that is deliberate, update ARCHIVER_SHA256 "
        "here, then copy the script to home-infra's scripts/luke/convmem-archive.sh and pin the "
        "same value in its scripts/check-convmem-archive.sh.")
