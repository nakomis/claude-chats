# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""Tests for facts/service.py. The model is always a fake: nothing here is paid for.

The database tests need a Postgres with pgvector where the role can CREATE DATABASE and
CREATE EXTENSION. Set FACTS_TEST_ADMIN_DSN (any maintenance database on it); each test
module run creates a uniquely named scratch database, applies init.sql, and drops it
afterwards. Without the variable those tests are skipped.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facts import service  # noqa: E402
from facts.extract import ExtractResult, Fact  # noqa: E402
from facts_eval import export  # noqa: E402

ADMIN_DSN = os.environ.get("FACTS_TEST_ADMIN_DSN")
needs_db = pytest.mark.skipif(not ADMIN_DSN, reason="FACTS_TEST_ADMIN_DSN not set")

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


# ---- no database ------------------------------------------------------------------

def test_config_comes_from_the_environment_only():
    cfg = service.Config.from_env({"FACTS_DATABASE_URL": "postgresql://x/y", "OLLAMA_BASE_URL": "http://embed:11434/",
                                   "FACTS_MAX_EXCHANGES": "7", "FACTS_SINCE": "2026-10-28"})
    assert (cfg.dsn, cfg.embed_url, cfg.max_exchanges, cfg.since) == ("postgresql://x/y", "http://embed:11434", 7, "2026-10-28")
    assert cfg.embed_tag == "ollama-mxbai-q8" and cfg.embed_model == "mxbai-q8" and cfg.embed_max_chars == 800
    assert service.Config.from_env({}).dsn == ""


def test_api_key_prefers_the_environment_then_ssm():
    class Ssm:
        def __init__(self):
            self.asked = []

        def get_parameter(self, Name, WithDecryption):
            self.asked.append((Name, WithDecryption))
            return {"Parameter": {"Value": "from-ssm"}}

    assert service.anthropic_api_key({"ANTHROPIC_API_KEY": "from-env"}, Ssm()) == "from-env"
    ssm = Ssm()
    assert service.anthropic_api_key({}, ssm) == "from-ssm"
    assert ssm.asked == [(service.DEFAULT_KEY_PARAM, True)]
    ssm = Ssm()
    service.anthropic_api_key({"FACTS_ANTHROPIC_KEY_PARAM": "/other/name"}, ssm)
    assert ssm.asked == [("/other/name", True)]


def test_service_and_eval_share_one_set_of_exchange_rules():
    rows = [("martin", "a", T0, 1), ("tool", "ls", T0, 2), ("claude", "b", T0, 3), ("claude", "b", T0, 4),
            ("martin", "c", T0, 5), ("claude", "d", T0, 6), ("martin", "e", T0, 7)]
    found = export.split_exchanges(rows)
    assert [e.messages for e in found] == export.build_exchanges(rows)
    assert [(e.first_seq, e.last_seq, e.closed) for e in found] == [(1, 3, True), (5, 6, True)]  # trailing Martin: no exchange yet
    assert [e.closed for e in export.split_exchanges(rows[:6])] == [True, False]  # open until Martin speaks again


# ---- scratch database -------------------------------------------------------------

@pytest.fixture(scope="module")
def dsn():
    import psycopg
    from psycopg import sql

    name = "facts_test_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
        info["dbname"] = name
        scratch = psycopg.conninfo.make_conninfo(**info)
        with psycopg.connect(scratch, autocommit=True) as c:
            c.execute((ROOT / "init.sql").read_text())
        yield scratch
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def conn(dsn):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("TRUNCATE facts_progress, fact_events, facts, messages, conversations CASCADE")
        yield c


def seed(conn, session, script, host="phi"):
    """script: [(author, content, minutes after T0)] -> conversation id; sequence_num counts from 1."""
    cid = conn.execute("INSERT INTO conversations (session_id, host) VALUES (%s, %s) RETURNING id",
                       (session, host)).fetchone()[0]
    for seq, (author, content, mins) in enumerate(script, 1):
        conn.execute(
            "INSERT INTO messages (conversation_id, message_uuid, role, content, created_at, sequence_num, author) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (cid, uuid.uuid4().hex, "assistant" if author == "claude" else "user", content,
             T0 + timedelta(minutes=mins), seq, author))
    return cid


class FakeExtractor:
    version = "fake-model+test"

    def __init__(self, facts_per=1, fail_on=()):
        self.calls, self.facts_per, self.fail_on = [], facts_per, set(fail_on)

    def extract(self, new_messages, observation_date, earlier_messages=(), already_extracted=()):
        self.calls.append(SimpleNamespace(messages=new_messages, date=observation_date,
                                          earlier=list(earlier_messages), already=list(already_extracted)))
        first = new_messages[0]["content"]
        if first in self.fail_on:
            return ExtractResult([], [{"uncached_in": 3, "output_tokens": 1}], "boom")
        facts = [Fact(f"{first} fact {i}", "config", "Mace", "ram", True, "2026-11-01") for i in range(self.facts_per)]
        return ExtractResult(facts, [{"uncached_in": 10, "cache_read": 100, "cache_write": 0, "output_tokens": 5}])


def fake_embed(text):
    return [0.5] * 1024


CFG = service.Config(embed_url="http://unused")
SCRIPT = [("martin", "m1", 0), ("tool", "ls", 1), ("claude", "c1", 2),
          ("martin", "m2", 10), ("claude", "c2", 11), ("martin", "m3", 20), ("claude", "c3", 21)]


def facts_of(conn):
    return conn.execute("SELECT text, first_seq, last_seq FROM facts ORDER BY first_seq, text").fetchall()


def progress(conn, cid):
    return conn.execute("SELECT last_seq, fail_seq, fail_count, gave_up FROM facts_progress WHERE conversation_id = %s",
                        (cid,)).fetchone()


@needs_db
def test_an_exchange_waits_until_it_is_complete(conn):
    # now() is real time, T0 is in the past: make the session look recent instead.
    recent = [(a, c, 0) for a, c, _ in SCRIPT]
    cid = seed(conn, "s-open", recent)
    conn.execute("UPDATE messages SET created_at = now() - interval '1 minute' WHERE conversation_id = %s", (cid,))
    ex = FakeExtractor()
    stats = service.run(conn, CFG, ex, fake_embed)
    assert stats.exchanges == 2 and stats.skipped_open == 1  # m3/c3 may still be growing
    assert progress(conn, cid)[0] == 5
    conn.execute("UPDATE messages SET created_at = now() - interval '31 minutes' WHERE conversation_id = %s", (cid,))
    stats = service.run(conn, CFG, ex, fake_embed)
    assert stats.exchanges == 1 and progress(conn, cid)[0] == 7


@needs_db
def test_facts_carry_their_provenance_and_run_is_idempotent(conn):
    cid = seed(conn, "s-1", SCRIPT, host="phi")
    ex = FakeExtractor()
    first = service.run(conn, CFG, ex, fake_embed)
    assert (first.exchanges, first.facts, first.errors, first.embedded) == (3, 3, 0, 3)
    assert (first.uncached_in, first.cache_read, first.output_tokens) == (30, 300, 15)
    assert facts_of(conn) == [("m1 fact 0", 1, 3), ("m2 fact 0", 4, 5), ("m3 fact 0", 6, 7)]
    row = conn.execute("SELECT host, extractor, kind, subject, attribute, temporary, expected_until, observed_at, "
                       "last_confirmed, conversation_id, embedder, embedder_board, embedder_fw, embedding IS NOT NULL "
                       "FROM facts WHERE text = 'm2 fact 0'").fetchone()
    assert row == ("phi", "fake-model+test", "config", "Mace", "ram", True, datetime(2026, 11, 1).date(),
                   T0 + timedelta(minutes=10), T0 + timedelta(minutes=10), cid, "ollama-mxbai-q8", None, None, True)
    # the model saw the exchange's London day, the messages before it and what was already extracted
    assert ex.calls[1].date == "2026-10-20"
    assert [m["content"] for m in ex.calls[1].earlier] == ["m1", "c1"]
    assert ex.calls[1].already == ["m1 fact 0"]
    assert progress(conn, cid) == (7, None, 0, 0)

    again = service.run(conn, CFG, ex, fake_embed)
    assert (again.exchanges, again.facts, again.embedded) == (0, 0, 0)
    assert len(facts_of(conn)) == 3 and len(ex.calls) == 3


@needs_db
def test_new_messages_continue_from_progress(conn):
    cid = seed(conn, "s-2", SCRIPT[:5])
    ex = FakeExtractor()
    service.run(conn, CFG, ex, fake_embed)
    assert progress(conn, cid)[0] == 5
    conn.execute("INSERT INTO messages (conversation_id, message_uuid, role, content, created_at, sequence_num, author) "
                 "VALUES (%s, 'u6', 'user', 'm3', %s, 6, 'martin'), (%s, 'u7', 'assistant', 'c3', %s, 7, 'claude')",
                 (cid, T0 + timedelta(minutes=20), cid, T0 + timedelta(minutes=21)))
    stats = service.run(conn, CFG, ex, fake_embed)
    assert stats.exchanges == 1 and len(facts_of(conn)) == 3


@needs_db
def test_a_failed_exchange_is_retried_then_given_up_without_blocking(conn):
    cid = seed(conn, "s-3", SCRIPT)
    ex = FakeExtractor(fail_on={"m2"})
    s1 = service.run(conn, CFG, ex, fake_embed)
    assert (s1.exchanges, s1.errors, s1.gave_up) == (1, 1, 0)
    assert progress(conn, cid) == (3, 4, 1, 0)       # stuck before m2, which is not marked processed
    assert [f[0] for f in facts_of(conn)] == ["m1 fact 0"]
    s2 = service.run(conn, CFG, ex, fake_embed)
    assert (s2.errors, progress(conn, cid)[2]) == (1, 2)
    s3 = service.run(conn, CFG, ex, fake_embed)      # third strike: logged, skipped, m3 goes through
    assert (s3.errors, s3.gave_up, s3.exchanges) == (1, 1, 1)
    assert progress(conn, cid) == (7, None, 0, 1)
    assert [f[0] for f in facts_of(conn)] == ["m1 fact 0", "m3 fact 0"]


@needs_db
def test_an_insert_failure_rolls_the_whole_exchange_back(conn):
    cid = seed(conn, "s-4", SCRIPT[:3])

    class BadSecondFact(FakeExtractor):
        def extract(self, *a, **k):
            r = super().extract(*a, **k)
            r.facts.append(Fact("never stored", "not-a-kind"))  # violates the kind CHECK
            return r

    stats = service.run(conn, CFG, BadSecondFact(), fake_embed)
    assert stats.errors == 1 and stats.exchanges == 0
    assert facts_of(conn) == []                       # the good fact rolled back with it
    assert progress(conn, cid) == (-1, 1, 1, 0)  # nothing done, one failure counted


@needs_db
def test_dry_run_and_limit(conn):
    cid = seed(conn, "s-5", SCRIPT)
    ex, lines = FakeExtractor(), []
    stats = service.run(conn, CFG, ex, None, limit=2, dry_run=True, out=lines.append)
    assert stats.exchanges == 2 and stats.dry_run
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM facts_progress").fetchone()[0] == 0
    assert ex.calls[1].already == ["m1 fact 0"]       # a dry run still threads its own facts through
    assert any("m2 fact 0" in l for l in lines)
    real = service.run(conn, CFG, ex, fake_embed, limit=2)
    assert real.exchanges == 2 and progress(conn, cid)[0] == 5


@needs_db
def test_since_ignores_older_messages(conn):
    seed(conn, "s-6", SCRIPT)
    cfg = service.Config(embed_url="x", since=(T0 + timedelta(minutes=5)).isoformat())
    stats = service.run(conn, cfg, FakeExtractor(), fake_embed)
    assert stats.exchanges == 2 and [f[0] for f in facts_of(conn)] == ["m2 fact 0", "m3 fact 0"]


@needs_db
def test_embedding_failures_leave_facts_for_the_next_run(conn):
    seed(conn, "s-7", SCRIPT)
    calls = []

    def flaky(text):
        calls.append(text)
        raise RuntimeError("cal is down")

    stats = service.run(conn, CFG, FakeExtractor(), flaky)
    assert stats.facts == 3 and stats.embedded == 0 and stats.embed_errors == 3
    assert conn.execute("SELECT count(*) FROM facts WHERE embedding IS NULL").fetchone()[0] == 3
    stats = service.run(conn, CFG, FakeExtractor(), fake_embed)
    assert stats.embedded == 3 and stats.exchanges == 0


@needs_db
def test_only_one_run_at_a_time(conn, dsn):
    import psycopg

    seed(conn, "s-8", SCRIPT)
    with psycopg.connect(dsn, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (service.LOCK_KEY,))
        assert service.run(conn, CFG, FakeExtractor(), fake_embed) is None
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
