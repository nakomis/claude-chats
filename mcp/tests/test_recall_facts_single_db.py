"""recall_facts on a single machine: one database, no replica (CHATS-38).

The single-machine edition registers the MCP server with CLAUDE_CHATS_DB_URL
only. CLAUDE_CHATS_READ_DB_URL, the distributed edition's read replica, is
never set, so every facts read must go to that one database and say so. The
facts here are inserted directly: extraction is CHATS-39, and until it runs
on a single machine the table is simply empty, which must not be an error.

The server is started as Claude Code starts it, a separate process with the
environment install.sh registers, so the module-level settings are read the
way they are in use. Embeddings come from the fake Ollama in support.py.

Needs CLAUDE_CHATS_TEST_ADMIN_DSN; skipped without it.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_DIR.parent / "hook" / "tests"))

from support import bag_of_words, fake_ollama, needs_db, scratch_database  # noqa: E402

pytestmark = needs_db

SCRIPT = """
import json, sys
from conversation_memory_mcp import server
assert server.READ_DB_URL is None, server.READ_DB_URL
calls = json.loads(sys.argv[1])
print(json.dumps([server.recall_facts(**args) for args in calls], default=str))
"""


def _recall(dsn: str, ollama_url: str, *calls: dict) -> list[dict]:
    # An allowlist, not the developer's shell: a CLAUDE_CHATS_*, a proxy or a
    # PG* variable there would change what is being tested.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "VIRTUAL_ENV")
           if k in os.environ}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    # What localhost/install.sh registers, and nothing else.
    env.update({"CLAUDE_CHATS_DB_URL": dsn, "CLAUDE_CHATS_PROVIDER": "ollama",
                "CLAUDE_CHATS_MODEL": "mxbai-embed-large", "CLAUDE_CHATS_DIMENSIONS": "1024",
                "OLLAMA_BASE_URL": ollama_url})
    out = subprocess.run([sys.executable, "-c", SCRIPT, json.dumps(list(calls))], env=env,
                         cwd=MCP_DIR, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _vec(text: str) -> str:
    return "[" + ",".join(str(v) for v in bag_of_words(text)) + "]"


def test_an_empty_facts_table_is_no_results_not_an_error():
    with scratch_database() as dsn, fake_ollama() as (url, _):
        (out,) = _recall(dsn, url, {"query": "how much RAM does the workshop server have"})
    assert out["status"] == "no_results"
    assert out["read_from"] == "primary"
    assert out["results"] == []


def test_a_seeded_fact_comes_back_from_the_one_database():
    import psycopg

    now = datetime.now(timezone.utc)
    with scratch_database() as dsn, fake_ollama() as (url, _):
        with psycopg.connect(dsn, autocommit=True) as conn:
            cid = conn.execute(
                "INSERT INTO conversations (session_id, project_path, host, name) "
                "VALUES ('facts-session', '/w/lathe', 'workbench', 'Lathe notes') "
                "RETURNING id").fetchone()[0]
            for text, kind, subject, attr, emb in (
                ("The workshop lathe is a Myford ML7", "identity", "lathe", "model", True),
                ("The bandsaw blade is 6 mm wide", "config", "bandsaw", "blade", True),
                # Not embedded yet: still found, by its words.
                ("The pillar drill needs a new belt", "situation", "drill", "belt", False),
            ):
                conn.execute(
                    "INSERT INTO facts (text, kind, subject, attribute, observed_at, "
                    "last_confirmed, conversation_id, first_seq, last_seq, host, extractor, "
                    "embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 1, 'workbench', "
                    "'test', %s::vector)",
                    (text, kind, subject, attr, now - timedelta(days=3), now - timedelta(days=1),
                     cid, _vec(text) if emb else None))

        lathe, by_vector, drill, this_host, other_host = _recall(
            dsn, url,
            {"query": "which lathe is in the workshop"},
            # "garage" is in no fact, and full-text needs every word, so only
            # the vector half can find anything here.
            {"query": "which lathe is in the workshop garage"},
            {"query": "pillar drill belt"},
            {"query": "which lathe is in the workshop", "host": "workbench"},
            {"query": "which lathe is in the workshop", "host": "elsewhere"})

    assert lathe["read_from"] == "primary"
    assert lathe["mode_used"] == "hybrid"
    # "ok", or "fallback" for exactly one reason: a pgvector without HNSW
    # iterative scans (before 0.8), as on some developers' local servers.
    if lathe["status"] == "fallback":
        assert lathe["warning"].startswith("pgvector HNSW iterative scans are unavailable")
    else:
        assert lathe["status"] == "ok", lathe
    top = lathe["results"][0]
    assert top["text"] == "The workshop lathe is a Myford ML7"
    assert top["kind"] == "identity" and top["host"] == "workbench"
    assert top["source"]["session_id"] == "facts-session"
    assert top["source"]["name"] == "Lathe notes"

    # Unembedded, so only the full-text half finds it; how it ranks against
    # the vector half's matches is decay's business, not this test's.
    assert "The pillar drill needs a new belt" in [r["text"] for r in drill["results"]]

    assert by_vector["results"][0]["text"] == "The workshop lathe is a Myford ML7"
    assert this_host["results"][0]["text"] == "The workshop lathe is a Myford ML7"
    assert other_host["results"] == []
