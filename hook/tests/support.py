"""Test helpers shared by the hook's and the MCP server's suites.

* ``fake_ollama()`` — a local HTTP server speaking just enough of Ollama's
  ``/api/embeddings`` for hook.embed, so nothing in a test needs a real model or
  the network. Its vectors are hashed bags of words: deterministic, and close
  for texts that share words, which is enough for search to find things.
* ``scratch_database()`` — a uniquely named database with init.sql applied,
  dropped afterwards. Needs CLAUDE_CHATS_TEST_ADMIN_DSN: a Postgres with
  pgvector where the role can CREATE DATABASE. Without it the tests that use
  it are skipped (``needs_db``), never failed.
"""

import contextlib
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = ROOT / "init.sql"
ADMIN_DSN = os.environ.get("CLAUDE_CHATS_TEST_ADMIN_DSN")
needs_db = pytest.mark.skipif(not ADMIN_DSN, reason="CLAUDE_CHATS_TEST_ADMIN_DSN not set")

DIMENSIONS = 1024


def bag_of_words(text: str) -> list[float]:
    vec = [0.0] * DIMENSIONS
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@contextlib.contextmanager
def fake_ollama(delay: float = 0.0):
    """Yields (base_url, calls): calls is the list of prompts embedded so far."""
    calls: list[str] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - the stdlib's name
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                calls.append(body["prompt"])
            if delay:
                time.sleep(delay)
            out = json.dumps({"embedding": bag_of_words(body["prompt"])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def scratch_database(schema: str | None = None):
    """Yields the DSN of a fresh database with ``schema`` (default init.sql) applied."""
    import psycopg
    from psycopg import sql

    name = "claude_chats_test_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
        info["dbname"] = name
        dsn = psycopg.conninfo.make_conninfo(**info)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(schema if schema is not None else INIT_SQL.read_text())
        yield dsn
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
