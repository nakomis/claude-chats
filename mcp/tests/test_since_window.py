"""The `since` search window, and what makes it safe on the vector path (CHATS-14).

`search_memory` searches the last 3 months unless told otherwise. Two things
have to hold for that to be an improvement rather than a trap:

  1. the window has to be expressible and overridable — including "no limit";
  2. the ivfflat index has to be scanned iteratively, because otherwise the ANN
     scan picks candidates by distance and the `created_at` predicate is applied
     afterwards, so the window silently eats most of the k results. Measured on
     Luke's 118k-row corpus: limit=10 with a 3-month window returned 10 rows
     with iterative_scan=relaxed_order and 0 rows with it off.

Deterministic: every case passes an explicit `now`. Runs under
`uv run --project . pytest` (or as a script).
"""

from datetime import datetime, timedelta, timezone

import pytest

from conversation_memory_mcp.server import (
    DEFAULT_SINCE,
    ITERATIVE_SCAN,
    _filters,
    _minus_months,
    _enable_iterative_scan,
    parse_since,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parsing the window
# ---------------------------------------------------------------------------

def test_default_is_three_months():
    assert DEFAULT_SINCE == "3 months"
    cutoff, label = parse_since(None, NOW)
    assert cutoff == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert label == "3 months"


def test_relative_periods():
    cases = {
        "30d":            NOW - timedelta(days=30),
        "30 days":        NOW - timedelta(days=30),
        "2w":             NOW - timedelta(weeks=2),
        "last 2 weeks":   NOW - timedelta(weeks=2),
        "6 hours":        NOW - timedelta(hours=6),
        "90 minutes":     NOW - timedelta(minutes=90),
        "1 year":         datetime(2025, 10, 15, 12, 0, tzinfo=UTC),
        "6mo":            datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
        "3 months ago":   datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        "PAST 1 Month":   datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
    }
    for text, expected in cases.items():
        cutoff, _ = parse_since(text, NOW)
        assert cutoff == expected, text


def test_no_limit_means_the_whole_corpus():
    for text in ("all", "ALL", "none", "ever", " forever ", "no limit", ""):
        cutoff, label = parse_since(text, NOW)
        assert cutoff is None, text
        assert "no limit" in label


def test_absolute_dates_and_timestamps():
    cutoff, label = parse_since("2026-01-15", NOW)
    # Naive input is read in the operator's zone, not UTC: someone typing a
    # bare date means their own midnight.
    assert cutoff.isoformat().startswith("2026-01-15T00:00:00")
    assert cutoff.utcoffset() == timedelta(0)  # January in Europe/London is GMT
    assert label.startswith("2026-01-15")

    cutoff, _ = parse_since("2026-06-01T09:30:00Z", NOW)
    assert cutoff == datetime(2026, 6, 1, 9, 30, tzinfo=UTC)


def test_month_arithmetic_clamps_the_day_rather_than_crashing():
    # 31 March minus one month is not 31 February.
    assert _minus_months(datetime(2026, 3, 31, tzinfo=UTC), 1) == datetime(2026, 2, 28, tzinfo=UTC)
    assert _minus_months(datetime(2024, 3, 31, tzinfo=UTC), 1) == datetime(2024, 2, 29, tzinfo=UTC)
    assert _minus_months(datetime(2026, 1, 15, tzinfo=UTC), 3) == datetime(2025, 10, 15, tzinfo=UTC)


def test_bare_m_is_rejected_because_it_is_ambiguous():
    # minutes and months differ by a factor of ~43,800; guessing is worse than
    # refusing.
    with pytest.raises(ValueError) as e:
        parse_since("30m", NOW)
    assert "min" in str(e.value) and "mo" in str(e.value)


def test_fractional_months_are_rejected_with_a_usable_message():
    with pytest.raises(ValueError) as e:
        parse_since("1.5 months", NOW)
    assert "days or weeks" in str(e.value)


def test_nonsense_is_rejected_and_the_message_names_the_accepted_forms():
    with pytest.raises(ValueError) as e:
        parse_since("whenever", NOW)
    msg = str(e.value)
    assert "3 months" in msg and "all" in msg


# ---------------------------------------------------------------------------
# Turning the window into SQL
# ---------------------------------------------------------------------------

def test_filters_emits_nothing_when_unbounded_and_unscoped():
    frag, params = _filters(None, None)
    assert frag == ""
    assert params == []


def test_filters_orders_fragment_and_params_together():
    cutoff = datetime(2026, 7, 15, tzinfo=UTC)
    frag, params = _filters("/repos/foo", cutoff)
    assert frag.index("c.project_path") < frag.index("m.created_at")
    assert params == ["/repos/foo", cutoff]


def test_the_date_bound_is_a_parameter_not_interpolated_sql():
    # The cutoff is computed in Python and bound, so no interval string from a
    # caller ever reaches the SQL text.
    frag, params = _filters(None, datetime(2026, 7, 15, tzinfo=UTC))
    assert "2026" not in frag
    assert frag.strip() == "AND m.created_at >= %s"
    assert isinstance(params[0], datetime)


def test_every_search_path_applies_the_same_filter():
    """A hybrid search whose halves disagreed about the window would return rows
    from outside it, so the fragment is built once and threaded through all
    three builders."""
    from conversation_memory_mcp.server import (
        _query_fulltext, _query_hybrid, _query_semantic,
    )

    cutoff = datetime(2026, 7, 15, tzinfo=UTC)
    where, where_params = _filters("/repos/foo", cutoff)

    class FakeCur:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))

        def fetchall(self):
            return []

    cur = FakeCur()
    _query_semantic(cur, "[0,1]", where, where_params, 5)
    _query_fulltext(cur, "q", where, where_params, 5)
    _query_hybrid(cur, "[0,1]", "q", where, where_params, 5, 50)

    sqls = [sql for sql, _ in cur.calls]
    assert all("m.created_at >= %s" in sql for sql in sqls)
    # Hybrid filters both halves, not just one.
    assert sqls[-1].count("m.created_at >= %s") == 2
    assert sqls[-1].count("c.project_path = %s") == 2
    # Every placeholder has a value, in the right order.
    for sql, params in cur.calls:
        assert sql.count("%s") == len(params), sql
    assert cur.calls[-1][1] == ["[0,1]", "/repos/foo", cutoff, "[0,1]", 50,
                                "q", "/repos/foo", cutoff, 50, 5]


# ---------------------------------------------------------------------------
# The ivfflat setting — the half that cannot be left to chance
# ---------------------------------------------------------------------------

class SpyCur:
    """Just enough cursor to watch what gets SET, and to answer SHOW."""

    def __init__(self, effective=ITERATIVE_SCAN, fail_on=None):
        self.effective, self.fail_on = effective, fail_on
        self.statements: list[str] = []
        self.connection = self
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            import psycopg
            raise psycopg.ProgrammingError("unrecognized configuration parameter")

    def fetchone(self):
        return (self.effective,)

    def rollback(self):
        self.rolled_back = True


def test_iterative_scan_is_set_and_then_read_back():
    cur = SpyCur()
    assert _enable_iterative_scan(cur) == ITERATIVE_SCAN
    assert f"SET ivfflat.iterative_scan = {ITERATIVE_SCAN}" in cur.statements
    # The read-back is the point: a SET that quietly did nothing would leave
    # date-filtered vector search under-returning with no symptom.
    assert "SHOW ivfflat.iterative_scan" in cur.statements


def test_pgvector_is_loaded_before_the_setting_is_touched():
    """Postgres accepts any value for a dotted GUC whose module is not loaded
    yet, and SHOW then echoes it back — `SET ivfflat.iterative_scan = banana`
    succeeds in a fresh session. Touching the vector type first makes both the
    SET and the read-back mean something, so it has to come first."""
    cur = SpyCur()
    _enable_iterative_scan(cur)
    assert "::vector" in cur.statements[0], cur.statements
    assert cur.statements[0].index("<=>") >= 0


def test_a_server_that_ignores_the_setting_is_reported_not_assumed():
    cur = SpyCur(effective="off")
    assert _enable_iterative_scan(cur) == "off"


def test_an_unknown_guc_rolls_back_rather_than_poisoning_the_transaction():
    # pgvector < 0.8 has no such setting; the failed SET aborts the transaction,
    # so search would die on its next statement unless we wind it back.
    cur = SpyCur(fail_on="SET ivfflat.iterative_scan")
    assert _enable_iterative_scan(cur) is None
    assert cur.rolled_back


def test_a_server_without_pgvector_at_all_is_survived():
    cur = SpyCur(fail_on="::vector")
    assert _enable_iterative_scan(cur) is None
    assert cur.rolled_back


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
