"""init.sql upgrades a database the old single-machine schema created (CHATS-36).

The single-machine edition used to carry its own, much older init.sql. It now
applies the repository's one schema on every install, so that schema has to
take a database created from the old copy forward without losing a row, and be
safe to re-apply after that. tests/fixtures/localhost-init-2026-09.sql is the
old copy. CHATS-37 goes further with a real old install; this is the floor.

Needs CLAUDE_CHATS_TEST_ADMIN_DSN (a Postgres with pgvector where the role can
CREATE DATABASE); skipped without it.
"""

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OLD_SCHEMA = ROOT / "tests" / "fixtures" / "localhost-init-2026-09.sql"
ADMIN_DSN = os.environ.get("CLAUDE_CHATS_TEST_ADMIN_DSN")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="CLAUDE_CHATS_TEST_ADMIN_DSN not set")


@pytest.fixture
def old_database():
    import psycopg
    from psycopg import sql

    name = "claude_chats_upgrade_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
        info["dbname"] = name
        dsn = psycopg.conninfo.make_conninfo(**info)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(OLD_SCHEMA.read_text())
            cid = conn.execute(
                "INSERT INTO conversations (session_id, project_path, host, name) "
                "VALUES ('old-session', '/p', 'old-host', 'Old name') RETURNING id").fetchone()[0]
            conn.execute(
                "INSERT INTO messages (conversation_id, message_uuid, role, content, sequence_num, embedding) "
                "VALUES (%s, 'old-msg', 'user', 'a message from the old days', 0, %s::vector)",
                (cid, "[" + ",".join(["0.01"] * 1024) + "]"))
        yield dsn
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def test_init_sql_upgrades_an_old_localhost_database_and_reapplies_cleanly(old_database):
    import psycopg

    schema = (ROOT / "init.sql").read_text()
    with psycopg.connect(old_database, autocommit=True) as conn:
        conn.execute(schema)
        conn.execute(schema)  # what every later install.sh run does

        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'messages'")}
        assert {"author", "tool_name", "model", "embedder", "embedder_board",
                "embedder_fw", "content_tsv"} <= cols
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        assert {"facts", "fact_events", "facts_progress"} <= tables

        row = conn.execute(
            "SELECT m.content, m.author, m.embedding IS NOT NULL, c.name, c.host "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id").fetchall()
        assert row == [("a message from the old days", "unknown", True, "Old name", "old-host")]

        # And a row carrying the new columns can be written.
        conn.execute(
            "INSERT INTO messages (conversation_id, message_uuid, role, author, content, sequence_num) "
            "SELECT id, 'new-msg', 'assistant', 'claude', 'new', 1 FROM conversations")
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
