"""The single-machine edition end to end: hook → outbox → drain → Postgres → MCP.

Run exactly as an install runs it: the hook as a separate process, with a
throwaway HOME holding the config.env install.sh would have written and
CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1 as on its command line, so the message
reaches Postgres through the drain the hook starts by itself. Embeddings come
from a fake Ollama (hook/tests/support.py), so nothing needs a model or the
network. Then the MCP server's own tools are asked to find it.

Needs CLAUDE_CHATS_TEST_ADMIN_DSN (see support.py); skipped without it.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOK_TESTS = Path(__file__).resolve().parents[2] / "hook" / "tests"
sys.path.insert(0, str(HOOK_TESTS))

from support import fake_ollama, needs_db, scratch_database  # noqa: E402

from conversation_memory_mcp import server  # noqa: E402

SESSION = "e2e-session-1"


def _transcript(path: Path) -> None:
    now = "2026-10-20T10:00:0{}+00:00"
    entries = [
        {"uuid": "u1", "timestamp": now.format(1),
         "message": {"role": "user", "content": "Why does the kettle in the shed trip the breaker?"}},
        {"uuid": "a1", "timestamp": now.format(2),
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "Let me look at the breaker log."},
             {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cat log"}},
         ]}},
        {"uuid": "t1", "timestamp": now.format(3),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1", "content": "RCD trip at 07:02"},
         ]}},
        {"uuid": "a2", "timestamp": now.format(4),
         "message": {"role": "assistant", "model": "claude-opus-5-5",
                     "content": [{"type": "text", "text": "The RCD tripped: the kettle element is leaking to earth."}]}},
        {"type": "custom-title", "customTitle": "Shed kettle"},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@needs_db
def test_a_captured_message_is_searchable_without_running_anything_else(tmp_path, monkeypatch):
    import psycopg

    home = tmp_path / "home"
    state = home / ".claude-chats"
    state.mkdir(parents=True)
    transcript = tmp_path / "transcript.jsonl"
    _transcript(transcript)

    with scratch_database() as dsn, fake_ollama() as (url, calls):
        (state / "config.env").write_text(
            "CLAUDE_CHATS_PROVIDER=ollama\n"
            "CLAUDE_CHATS_MODEL=mxbai-embed-large\n"
            f"CLAUDE_CHATS_DB_URL={dsn}\n"
            f"OLLAMA_BASE_URL={url}\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CHATS_", "OLLAMA_"))}
        env.update(HOME=str(home), CLAUDE_CHATS_DRAIN_AFTER_CAPTURE="1", CLAUDE_CHATS_HOST="e2e-host")
        payload = json.dumps({"session_id": SESSION, "transcript_path": str(transcript),
                              "cwd": "/home/me/shed", "git_branch": "main"})
        hook = subprocess.run([sys.executable, "-m", "hook.record"], input=payload,
                              capture_output=True, text=True, env=env, timeout=60)
        assert hook.returncode == 0, hook.stderr

        # Nothing else is run: the drain the hook started has to deliver.
        deadline = time.monotonic() + 60
        rows = []
        while time.monotonic() < deadline:
            with psycopg.connect(dsn) as conn:
                rows = conn.execute(
                    "SELECT m.message_uuid, m.author, m.tool_name, m.model, m.embedding IS NOT NULL, "
                    "c.name, c.host FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "ORDER BY m.sequence_num").fetchall()
            if len(rows) == 4:
                break
            time.sleep(0.25)
        log = state / "drain.log"
        assert len(rows) == 4, log.read_text() if log.exists() else "no drain.log"
        assert [r[:4] for r in rows] == [
            ("u1", "martin", None, None),
            ("a1", "claude", None, "claude-opus-5-5"),
            ("t1", "tool", "Bash", None),
            ("a2", "claude", None, "claude-opus-5-5"),
        ]
        assert all(r[4] for r in rows)
        assert {(r[5], r[6]) for r in rows} == {("Shed kettle", "e2e-host")}

        # A later drain (the timer's) finds nothing and says so with exit 0.
        again = subprocess.run([sys.executable, "-m", "hook.drain", "--env-file", str(state / "config.env")],
                               capture_output=True, text=True, env=env, timeout=60)
        assert again.returncode == 0, again.stderr

        # And the MCP server finds it, through its own tools.
        monkeypatch.setattr(server, "DB_URL", dsn)
        # The ivfflat index was trained on an empty table, so with four rows
        # most of its 100 lists are empty; visit them all.
        monkeypatch.setattr(server, "IVFFLAT_PROBES", 100)
        monkeypatch.setenv("OLLAMA_BASE_URL", url)
        found = server.search_memory("kettle breaker RCD", since="all", limit=3)
        assert found["status"] in ("ok", "fallback"), found
        assert found["results"] and found["results"][0]["session_id"] == SESSION
        assert found["results"][0]["name"] == "Shed kettle"

        for mode in ("semantic", "fulltext"):
            hit = server.search_memory("kettle", since="all", mode=mode)
            assert hit["results"], (mode, hit)

        convo = server.get_conversation(SESSION)
        assert [m["seq"] for m in convo["messages"]] == [0, 1, 2, 3]

        sessions = server.list_recent_sessions()
        assert [s["session_id"] for s in sessions] == [SESSION]


def test_the_mcp_server_embeds_with_the_hooks_helper():
    """One embed.py: the server's helper is the hook's, not a copy of it."""
    import hook.embed

    assert server.get_embedding is hook.embed.get_embedding
