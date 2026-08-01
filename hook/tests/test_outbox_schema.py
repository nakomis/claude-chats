"""Guard the outbox DDL against drift from the Rust forwarder's copy.

The outbox schema is defined twice — in this hook's ``record.py`` and in the
Rust forwarder's ``outbox.rs`` (``mac/conversation-memory-forwarder`` in the
``nakomis/home-infra`` repo) — because the two live in separate repositories
and cannot share a build-time file. Both processes create the table with
``IF NOT EXISTS``, so whichever starts first wins and a drifted copy on the
other side is silently ignored. That is exactly the kind of divergence that had
already lost fields on the SQS wire (HOME-194).

This pins a checksum of the normalised DDL. An accidental edit fails the test
loudly; the *same* value is pinned in the forwarder's Rust test, so keeping both
suites green forces any real schema change to be made — deliberately — in both
repos at once.

If you change the schema on purpose: apply the identical change to the
forwarder's ``outbox.rs`` and update both pinned hashes (here and in that test)
to the new value printed by the failing assertion, in the same change.

Runs standalone (``python hook/tests/test_outbox_schema.py``) or under pytest.
Uses only the standard library.
"""

import hashlib
import os
import sys

# Make the `hook` package importable whether run directly or under pytest: add
# the directory that contains the `hook/` package (two levels up from this file).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook.record import SCHEMA  # noqa: E402

# Must equal SCHEMA_SHA256 in the forwarder's outbox.rs test. Same algorithm
# (sha256), same normalisation, so the two are the same value by construction.
SCHEMA_SHA256 = "ed79497fb291e0efff6baa3d3498bb15760143259ef2707d2e943706d5508968"


def _normalise(s: str) -> str:
    """Trim trailing whitespace per line, drop blank lines, join with ``\\n``.

    Language-agnostic so cosmetic whitespace differences between the Python and
    Rust copies don't matter, only the DDL does. The forwarder's Rust test does
    exactly the same.
    """
    lines = [line.rstrip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def test_schema_matches_the_pinned_checksum():
    digest = hashlib.sha256(_normalise(SCHEMA).encode()).hexdigest()
    assert digest == SCHEMA_SHA256, (
        "outbox DDL changed. If deliberate, apply the identical change to the "
        "Rust forwarder's outbox.rs SCHEMA and update both pinned hashes (here "
        f"and in that test) to this value, in the same change: {digest}"
    )


if __name__ == "__main__":
    test_schema_matches_the_pinned_checksum()
    print("ok: outbox schema matches the pinned checksum", SCHEMA_SHA256)
