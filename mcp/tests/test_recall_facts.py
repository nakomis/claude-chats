"""recall_facts: decay, labels, presentation and SQL construction (CHATS-26).

Deterministic and database-free: every case passes an explicit `now`. The real
Postgres run lives in scripts/recall-facts-scratch-check.py. Runs under
`uv run --project . pytest` (or as a script).
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from conversation_memory_mcp import server
from conversation_memory_mcp.server import (
    FACT_HALF_LIFE_DAYS,
    _fact_filters,
    _facts_conn,
    _facts_params,
    _facts_sql,
    decay_factor,
    fact_age_phrase,
    fact_labels,
    present_fact,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------

def test_half_lives_are_the_documented_ones():
    assert FACT_HALF_LIFE_DAYS["identity"] == pytest.approx(365.25 * 10)
    assert FACT_HALF_LIFE_DAYS["config"] == pytest.approx(365.25 * 0.75)
    assert FACT_HALF_LIFE_DAYS["situation"] == 14
    assert FACT_HALF_LIFE_DAYS["event"] is None


def test_decay_halves_at_one_half_life():
    assert decay_factor("situation", NOW - timedelta(days=14), NOW) == pytest.approx(0.5)
    assert decay_factor("situation", NOW - timedelta(days=28), NOW) == pytest.approx(0.25)
    one_config = NOW - timedelta(days=FACT_HALF_LIFE_DAYS["config"])
    assert decay_factor("config", one_config, NOW) == pytest.approx(0.5)


def test_decay_is_one_when_just_confirmed():
    assert decay_factor("situation", NOW, NOW) == 1.0


def test_events_never_decay():
    assert decay_factor("event", NOW - timedelta(days=36500), NOW) == 1.0


def test_future_last_confirmed_is_clamped():
    assert decay_factor("situation", NOW + timedelta(days=3), NOW) == 1.0


def test_naive_last_confirmed_is_read_as_utc():
    naive = (NOW - timedelta(days=14)).replace(tzinfo=None)
    assert decay_factor("situation", naive, NOW) == pytest.approx(0.5)


def test_kinds_decay_at_different_speeds():
    then = NOW - timedelta(days=60)
    factors = [decay_factor(k, then, NOW) for k in ("situation", "config", "identity", "event")]
    assert factors == sorted(factors)


# ---------------------------------------------------------------------------
# Labels and the age phrase
# ---------------------------------------------------------------------------

def test_plain_fact_has_no_labels():
    assert fact_labels(valid_to=None, expected_until=None, temporary=False, now=NOW) == []


def test_possibly_resolved_when_expected_until_has_passed():
    labels = fact_labels(valid_to=None, expected_until=date(2026, 10, 1), temporary=True, now=NOW)
    assert labels == ["temporary", "possibly resolved"]


def test_not_resolved_while_expected_until_is_ahead_or_today():
    for d in (date(2026, 10, 15), date(2026, 11, 1)):
        labels = fact_labels(valid_to=None, expected_until=d, temporary=True, now=NOW)
        assert "possibly resolved" not in labels


def test_superseded_label_and_no_possibly_resolved_for_superseded():
    labels = fact_labels(valid_to=NOW, expected_until=date(2026, 1, 1), temporary=True, now=NOW)
    assert labels == ["superseded", "temporary"]


def test_age_phrase_stated_and_confirmed():
    assert fact_age_phrase(datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
                           datetime(2026, 10, 3, 10, 0, tzinfo=UTC)) \
        == "stated 27 Sep 2026, confirmed 3 Oct 2026"


def test_age_phrase_never_reconfirmed():
    ts = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    assert fact_age_phrase(ts, ts + timedelta(hours=2)) == "stated 27 Sep 2026, not re-confirmed since"


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _row(**over):
    base = dict(
        id=uuid4(), text="Mace has 16 GB of RAM", kind="config", subject="Mace",
        attribute="RAM", observed_at=NOW - timedelta(days=18),
        last_confirmed=NOW - timedelta(days=12), valid_to=None, temporary=False,
        expected_until=None, first_seq=4, last_seq=9, source=None, host="mu",
        session_id="sess-1", name=None, ai_title="Sizing Mace", project_path="/p",
        successor_text=None, successor_observed_at=None,
    )
    base.update(over)
    return tuple(base.values())


def test_conversation_fact_formatting():
    out = present_fact(_row(), 0.03, NOW)
    assert out["text"] == "Mace has 16 GB of RAM"
    assert out["kind"] == "config" and out["subject"] == "Mace" and out["attribute"] == "RAM"
    assert out["age"] == "stated 27 Sep 2026, confirmed 3 Oct 2026"
    assert out["observed_at"].startswith("3 weeks ago")
    assert out["last_confirmed"].startswith("2 weeks ago")
    assert out["still_true_pct"] == 97
    assert out["labels"] == []
    assert "superseded" not in out and "temporary" not in out
    assert out["source"] == {"session_id": "sess-1", "name": None, "ai_title": "Sizing Mace",
                             "project_path": "/p", "first_seq": 4, "last_seq": 9}
    assert out["score"] == pytest.approx(0.03 * decay_factor("config", NOW - timedelta(days=12), NOW), abs=1e-6)


def test_doc_fact_reports_source_not_session():
    out = present_fact(_row(session_id=None, ai_title=None, project_path=None,
                            first_seq=None, last_seq=None, source="docs/hosts.md"), 0.02, NOW)
    assert out["source"] == {"source": "docs/hosts.md"}


def test_temporary_fact_shows_expected_until_and_possibly_resolved():
    out = present_fact(_row(kind="situation", temporary=True, expected_until=date(2026, 10, 1)), 0.02, NOW)
    assert out["temporary"] is True
    assert out["expected_until"] == "2026-10-01"
    assert out["labels"] == ["temporary", "possibly resolved"]


def test_superseded_fact_shows_successor_and_zero_probability():
    out = present_fact(_row(valid_to=NOW - timedelta(days=2),
                            successor_text="Mace has 32 GB of RAM",
                            successor_observed_at=NOW - timedelta(days=2)), 0.02, NOW)
    assert out["labels"] == ["superseded"]
    assert out["still_true_pct"] == 0
    assert out["superseded"]["by"]["text"] == "Mace has 32 GB of RAM"
    assert out["superseded"]["valid_to"].startswith("2 days ago")


def test_event_probability_stays_100():
    out = present_fact(_row(kind="event", last_confirmed=NOW - timedelta(days=3000)), 0.02, NOW)
    assert out["still_true_pct"] == 100


# ---------------------------------------------------------------------------
# SQL construction
# ---------------------------------------------------------------------------

def test_default_filters_hide_deleted_and_superseded():
    frag, params = _fact_filters(None, None, None, None, include_history=False)
    assert "f.deleted_at IS NULL" in frag
    assert "f.valid_to IS NULL" in frag
    assert params == []


def test_history_keeps_superseded_but_never_deleted():
    frag, _ = _fact_filters(None, None, None, None, include_history=True)
    assert "f.deleted_at IS NULL" in frag
    assert "valid_to" not in frag


def test_each_filter_adds_its_clause_and_param_in_order():
    since = NOW - timedelta(days=30)
    frag, params = _fact_filters(since, "mu", "/proj", "config", include_history=False)
    assert frag.index("last_confirmed") < frag.index("host") < frag.index("project_path") < frag.index("f.kind")
    assert params == [since, "mu", "/proj", "config"]
    assert frag.count("%s") == len(params)


def test_hybrid_sql_placeholders_match_params():
    frag, wparams = _fact_filters(NOW, "mu", "/proj", "event", include_history=False)
    sql = _facts_sql(True, frag)
    params = _facts_params("[0.1]", "ram", wparams, 100)
    assert sql.count("%s") == len(params)
    assert "semantic AS" in sql and "FULL OUTER JOIN fulltext" in sql
    assert "f.embedding <=> %s::vector" in sql and "f.text_tsv @@ q" in sql
    assert params[0] == "[0.1]" and params[len(wparams) + 3] == "ram"


def test_fulltext_only_sql_has_no_vector_half():
    frag, wparams = _fact_filters(None, None, "/proj", None, include_history=False)
    sql = _facts_sql(False, frag)
    params = _facts_params(None, "ram", wparams, 100)
    assert sql.count("%s") == len(params)
    assert "semantic" not in sql and "<=>" not in sql
    assert params == ["ram", "/proj", 100]


# ---------------------------------------------------------------------------
# Read DSN
# ---------------------------------------------------------------------------

class _FakePsycopg:
    def __init__(self, failing):
        self.failing, self.calls = failing, []

    def connect(self, dsn, **kw):
        self.calls.append(dsn)
        if dsn in self.failing:
            raise server.psycopg.OperationalError("down")
        return f"conn:{dsn}"


def _patch(monkeypatch, read, failing=()):
    fake = _FakePsycopg(failing)
    monkeypatch.setattr(server, "READ_DB_URL", read)
    monkeypatch.setattr(server, "DB_URL", "primary-dsn")
    monkeypatch.setattr(server.psycopg, "connect", fake.connect)
    return fake


def test_no_read_dsn_uses_primary(monkeypatch):
    fake = _patch(monkeypatch, None)
    assert _facts_conn() == ("conn:primary-dsn", "primary")
    assert fake.calls == ["primary-dsn"]


def test_read_dsn_used_when_reachable(monkeypatch):
    _patch(monkeypatch, "replica-dsn")
    assert _facts_conn() == ("conn:replica-dsn", "replica")


def test_unreachable_replica_falls_back_to_primary(monkeypatch):
    fake = _patch(monkeypatch, "replica-dsn", failing={"replica-dsn"})
    assert _facts_conn() == ("conn:primary-dsn", "primary")
    assert fake.calls == ["replica-dsn", "primary-dsn"]


# ---------------------------------------------------------------------------
# Argument validation (no database reached)
# ---------------------------------------------------------------------------

def test_bad_kind_and_bad_since_are_errors():
    bad_kind = server.recall_facts("x", kind="banana")
    assert bad_kind["status"] == "error" and "identity" in bad_kind["warning"]
    bad_since = server.recall_facts("x", since="3m")
    assert bad_since["status"] == "error"
