"""Timestamp presentation (HOME-199).

The conversation-memory MCP used to return `created_at` as a bare UTC clock
time (e.g. 21:37), which a reader on BST wall-clock (22:37) misreads as an hour
old. These helpers present the relative form plus operator-local time so it
can't be misread, while keeping the raw UTC available separately.

Deterministic: every case passes an explicit `now`, so there's no wall-clock
flakiness. Runs under `uv run --project . pytest` (or as a script).
"""

from datetime import datetime, timezone

from conversation_memory_mcp.server import _relative, _human_time, _utc_iso

UTC = timezone.utc


def test_relative_scales():
    now = datetime(2026, 7, 24, 22, 39, tzinfo=UTC)
    assert _relative(datetime(2026, 7, 24, 22, 39, 30, tzinfo=UTC), now) == "just now"
    assert _relative(datetime(2026, 7, 24, 22, 38, tzinfo=UTC), now) == "1 minute ago"
    assert _relative(datetime(2026, 7, 24, 22, 37, tzinfo=UTC), now) == "2 minutes ago"
    assert _relative(datetime(2026, 7, 24, 21, 39, tzinfo=UTC), now) == "1 hour ago"
    assert _relative(datetime(2026, 7, 23, 22, 39, tzinfo=UTC), now) == "1 day ago"
    assert _relative(datetime(2026, 7, 10, 22, 39, tzinfo=UTC), now) == "2 weeks ago"


def test_relative_handles_clock_skew_into_the_future():
    now = datetime(2026, 7, 24, 22, 39, tzinfo=UTC)
    assert _relative(datetime(2026, 7, 24, 22, 42, tzinfo=UTC), now) == "in 3 minutes"


def test_human_time_is_the_exact_scenario_that_confused_a_session():
    # The message stored 21:37 UTC was ~2 min old: real now was 22:39 BST, i.e.
    # 21:39 UTC. A session instead compared 21:37 against its 22:39 BST wall
    # clock and reported "an hour ago". Now it leads with the correct relative
    # form and shows 22:37 BST — no misread possible.
    now = datetime(2026, 7, 24, 21, 39, tzinfo=UTC)
    got = _human_time(datetime(2026, 7, 24, 21, 37, tzinfo=UTC), now)
    assert got == "2 minutes ago (2026-07-24 22:37 BST)", got


def test_human_time_uses_gmt_in_winter():
    # Same UK zone, but January → GMT (no DST), so 21:37 UTC stays 21:37 GMT.
    now = datetime(2026, 1, 15, 21, 39, tzinfo=UTC)
    got = _human_time(datetime(2026, 1, 15, 21, 37, tzinfo=UTC), now)
    assert got == "2 minutes ago (2026-01-15 21:37 GMT)", got


def test_utc_iso_preserves_the_exact_value():
    assert _utc_iso(datetime(2026, 7, 24, 21, 37, tzinfo=UTC)) == "2026-07-24T21:37:00+00:00"


def test_naive_timestamp_is_treated_as_utc():
    # Defensive: if psycopg ever hands back a naive datetime, assume UTC rather
    # than crashing on astimezone.
    now = datetime(2026, 7, 24, 21, 39, tzinfo=UTC)
    assert _human_time(datetime(2026, 7, 24, 21, 37), now) == "2 minutes ago (2026-07-24 22:37 BST)"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok:", name)
