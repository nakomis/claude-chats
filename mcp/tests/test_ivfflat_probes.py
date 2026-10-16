"""ivfflat.probes on the vector search path (CHATS-15).

pgvector's default of 1 visits one of the index's 100 lists and, measured on
Luke, returned 0.58 of the true top 10. The MCP sets 20 (overridable) and reads
it back, so a setting that silently did nothing is reported, not assumed.

Runs under `uv run --project . pytest` (or as a script).
"""

import pytest

from conversation_memory_mcp.server import (
    IVFFLAT_PROBES,
    _probes_from_env,
    _set_probes,
)


class SpyCur:
    """Just enough cursor to watch what gets SET, and to answer SHOW."""

    def __init__(self, effective=str(IVFFLAT_PROBES), fail_on=None):
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


def test_default_is_20():
    assert _probes_from_env(None) == 20
    assert _probes_from_env("") == 20
    assert _probes_from_env("  ") == 20


def test_env_override_is_taken():
    assert _probes_from_env("10") == 10
    assert _probes_from_env(" 32768 ") == 32768


@pytest.mark.parametrize("raw", ["0", "-3", "32769"])
def test_out_of_range_is_rejected(raw):
    with pytest.raises(ValueError, match="1-32768"):
        _probes_from_env(raw)


@pytest.mark.parametrize("raw", ["twenty", "20; DROP TABLE messages", "2.5"])
def test_non_integers_are_rejected(raw):
    # The value is interpolated into SET, so it must never be anything but an int.
    with pytest.raises(ValueError, match="must be an integer"):
        _probes_from_env(raw)


def test_probes_is_set_and_then_read_back():
    cur = SpyCur()
    assert _set_probes(cur) == IVFFLAT_PROBES
    assert cur.statements == [f"SET ivfflat.probes = {IVFFLAT_PROBES}", "SHOW ivfflat.probes"]


def test_a_server_that_ignores_the_setting_is_reported_not_assumed():
    assert _set_probes(SpyCur(effective="1")) == 1


def test_a_failed_set_rolls_back_rather_than_poisoning_the_transaction():
    cur = SpyCur(fail_on="SET ivfflat.probes")
    assert _set_probes(cur) is None
    assert cur.rolled_back


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
