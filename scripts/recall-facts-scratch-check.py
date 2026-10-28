#!/usr/bin/env python3
"""Run recall_facts against a throwaway Postgres database (CHATS-26).

Creates a uniquely named scratch database on the server the admin DSN points at,
applies the repo's init.sql to it, inserts a handful of conversations and facts
(embedded with the same embedder the MCP uses), exercises recall_facts through
its real code path, then drops the scratch database in a finally block. It never
connects to claude_chats itself.

    CLAUDE_CHATS_ADMIN_DSN=postgresql://USER:PASSWORD@HOST:PORT/postgres \\
        uv run --project mcp python scripts/recall-facts-scratch-check.py

The admin DSN's role needs CREATEDB. Exits non-zero if any check fails.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))

ADMIN_DSN = os.environ.get("CLAUDE_CHATS_ADMIN_DSN")
NOW = datetime.now(timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def with_dbname(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path="/" + name))


def dead_port(dsn: str) -> str:
    """The same DSN pointed at port 1, where nothing listens."""
    parts = urlsplit(dsn)
    userinfo = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=(userinfo + "@" if userinfo else "") + f"{parts.hostname}:1"))


# (key, text, kind, subject, attribute, observed days ago, confirmed days ago,
#  valid_to days ago | None, superseded by key | None, expected_until offset | None,
#  conversation key | None, source, host, deleted)
FACTS = [
    ("ram_old", "Mace has 8 GB of RAM", "config", "Mace", "RAM", 200, 200, 30, "ram_new", None, "c1", None, "host-a", False),
    ("ram_new", "Mace has 32 GB of RAM since the upgrade", "config", "Mace", "RAM", 30, 30, None, None, None, "c1", None, "host-a", False),
    ("cat", "Tau is a black and white tuxedo cat who loves radiators", "identity", "Tau", "appearance", 900, 900, None, None, None, "c2", None, "host-b", False),
    ("away", "Martin is away on holiday in Cornwall", "situation", "Martin", "location", 40, 40, None, None, -20, "c2", None, "host-a", False),
    ("fix", "The Mace disk was replaced after the SMART failure", "event", "Mace", "disk", 1000, 1000, None, None, None, "c1", None, "host-a", False),
    ("doc", "Host Alpha runs Postgres on port 5432 for conversation memory", "config", "Alpha", "postgres port", 10, 10, None, None, None, None, "docs/hosts.md", None, False),
    ("gone", "Mace has 64 GB of RAM", "config", "Mace", "RAM", 5, 5, None, None, None, "c1", None, "host-a", True),
]


def main() -> int:
    if not ADMIN_DSN:
        print("usage: CLAUDE_CHATS_ADMIN_DSN=postgresql://USER:PASSWORD@HOST:PORT/postgres "
              "uv run --project mcp python scripts/recall-facts-scratch-check.py\n"
              "CLAUDE_CHATS_ADMIN_DSN is required: a role with CREATEDB on the server to test against.",
              file=sys.stderr)
        return 2
    from conversation_memory_mcp import server
    from conversation_memory_mcp.embed import get_embedding

    scratch = "recall_scratch_" + uuid.uuid4().hex[:12]
    assert scratch != "claude_chats"
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail and not ok else ""))
        if not ok:
            failures.append(label)

    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{scratch}"')
        print(f"created scratch database {scratch}")
        dsn = with_dbname(ADMIN_DSN, scratch)

        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute((ROOT / "init.sql").read_text())
            convs = {}
            for key, sess, proj, host in (("c1", "sess-mace", "/repos/mace", "host-a"),
                                          ("c2", "sess-cats", "/repos/cats", "host-b")):
                convs[key] = db.execute(
                    "INSERT INTO conversations (session_id, project_path, ai_title, host) "
                    "VALUES (%s, %s, %s, %s) RETURNING id", (sess, proj, f"title of {sess}", host)
                ).fetchone()[0]
            ids = {}
            for (key, text, kind, subj, attr, obs, conf, vt, _sup, exp, conv, src, host, deleted) in FACTS:
                ids[key] = db.execute(
                    """INSERT INTO facts (text, kind, subject, attribute, observed_at, last_confirmed,
                                          valid_to, temporary, expected_until, conversation_id,
                                          first_seq, last_seq, source, host, extractor, embedding, deleted_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'scratch',%s::vector,%s)
                       RETURNING id""",
                    (text, kind, subj, attr, days_ago(obs), days_ago(conf),
                     days_ago(vt) if vt else None, exp is not None,
                     (NOW + timedelta(days=exp)).date() if exp is not None else None,
                     convs[conv] if conv else None, 3 if conv else None, 7 if conv else None,
                     src, host, server._vec(get_embedding(text)),
                     NOW if deleted else None),
                ).fetchone()[0]
            for (key, *_r) in FACTS:
                sup = next(f[8] for f in FACTS if f[0] == key)
                if sup:
                    db.execute("UPDATE facts SET superseded_by = %s WHERE id = %s", (ids[sup], ids[key]))

        # Point the MCP at the scratch database, with a dead replica DSN to
        # exercise the fallback as well.
        server.DB_URL = dsn
        server.READ_DB_URL = dead_port(dsn)

        def texts(res):
            return [r["text"] for r in res["results"]]

        r = server.recall_facts("Mace RAM")
        check("status ok, hybrid", r["status"] == "ok" and r["mode_used"] == "hybrid", str(r.get("warning")))
        check("dead replica falls back to primary", r["read_from"] == "primary")
        check("current RAM fact ranks first", texts(r)[:1] == ["Mace has 32 GB of RAM since the upgrade"], str(texts(r)))
        check("superseded fact hidden by default", "Mace has 8 GB of RAM" not in texts(r))
        check("soft-deleted fact never returned", "Mace has 64 GB of RAM" not in texts(r))
        top = r["results"][0]
        check("source points at the conversation",
              top["source"] == {"session_id": "sess-mace", "name": None, "ai_title": "title of sess-mace",
                                "project_path": "/repos/mace", "first_seq": 3, "last_seq": 7}, str(top["source"]))
        check("age phrase present", top["age"].startswith("stated "), top["age"])

        h = server.recall_facts("Mace RAM", include_history=True)
        old = next((x for x in h["results"] if x["text"] == "Mace has 8 GB of RAM"), None)
        check("history returns the superseded fact with its successor",
              old is not None and old["superseded"]["by"]["text"].startswith("Mace has 32 GB"), str(old))
        check("history still hides soft-deleted", "Mace has 64 GB of RAM" not in texts(h))

        a = server.recall_facts("Martin holiday Cornwall")
        away = next((x for x in a["results"] if "Cornwall" in x["text"]), None)
        check("expired temporary fact is possibly resolved",
              away is not None and "possibly resolved" in away["labels"] and away["expected_until"], str(away))
        check("situation decays hard (40 days, 2-week half-life)", away is not None and away["still_true_pct"] <= 15,
              str(away and away["still_true_pct"]))

        c = server.recall_facts("Tau cat")
        tau = next((x for x in c["results"] if x["kind"] == "identity"), None)
        check("900-day-old identity fact still around 84%", tau is not None and tau["still_true_pct"] >= 80,
              str(tau and tau["still_true_pct"]))

        d = server.recall_facts("disk replaced SMART")
        ev = next((x for x in d["results"] if x["kind"] == "event"), None)
        check("event does not decay", ev is not None and ev["still_true_pct"] == 100)

        doc = server.recall_facts("Postgres port on Alpha")
        dr = next((x for x in doc["results"] if "Alpha" in x["text"]), None)
        check("doc fact reports its source", dr is not None and dr["source"] == {"source": "docs/hosts.md"}, str(dr))

        k = server.recall_facts("Mace", kind="event")
        check("kind filter", k["results"] and all(x["kind"] == "event" for x in k["results"]), str(texts(k)))
        p = server.recall_facts("Mace", project_path="/repos/cats")
        check("project_path filter excludes other projects and doc facts",
              all(x["source"].get("session_id") == "sess-cats" for x in p["results"]), str(texts(p)))
        hs = server.recall_facts("Mace Alpha Tau", host="host-b")
        check("host filter", hs["results"] and all(x["host"] == "host-b" for x in hs["results"]), str(texts(hs)))
        s = server.recall_facts("Mace", since="60 days")
        check("since filter drops facts last confirmed earlier",
              "The Mace disk was replaced after the SMART failure" not in texts(s) and texts(s), str(texts(s)))
        check("limit honoured", len(server.recall_facts("Mace Alpha Tau", limit=2)["results"]) == 2)

        # Embedder down: full-text fallback.
        orig = server.get_embedding
        server.get_embedding = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            f = server.recall_facts("Mace RAM")
            check("fulltext fallback when the embedder is down",
                  f["mode_used"] == "fulltext" and f["status"] == "fallback" and f["results"], str(f.get("warning")))
        finally:
            server.get_embedding = orig

        # Working replica path: same scratch DB via a live "replica" DSN.
        server.READ_DB_URL = dsn
        check("live read DSN is used", server.recall_facts("Mace")["read_from"] == "replica")
    finally:
        # Terminate stragglers, then drop: the scratch name is the only thing ever dropped.
        try:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = %s AND pid <> pg_backend_pid()", (scratch,))
            admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
            print(f"dropped scratch database {scratch}")
        finally:
            admin.close()

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
