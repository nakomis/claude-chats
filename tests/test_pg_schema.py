"""Guard the pg schema against drift from its vendored copy on Luke.

``init.sql`` here is the source of truth for the conversation-memory pg schema.
It is *vendored* into the home-infra repo at
``luke/deployment/claude-chats/init.sql``, which is what actually runs on first
init of Luke's database. Nothing mechanically keeps the copy in step with this
source — the third of the three contract-duplications tracked by HOME-194.

This pins a checksum of the DDL. The home-infra side pins the *same* value with
the *same* normalisation (its scripts/check-convmem-pg-schema.sh, run as a CI
job), so an edit here fails this test and an un-mirrored edit there fails that
CI job. Change the schema and BOTH go red until both copies and both pinned
hashes are updated, together, to the new value.

The vendored copy intentionally carries a different header comment, so the
checksum is over the DDL only: full-line ``--`` comments and blank lines are
stripped before hashing.

Runs standalone (``python tests/test_pg_schema.py``) or under pytest. Stdlib
only.
"""

import hashlib
import os

SCHEMA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "init.sql"
)

# Must equal EXPECTED in home-infra's scripts/check-convmem-pg-schema.sh. Same
# algorithm (sha256), same normalisation, so it is the same value by
# construction.
SCHEMA_SHA256 = "95e90ad32b46243c1b23e97b67f5d191ff96ef39717f4effbe905ace31f5e9f0"


def _normalise_sql(s: str) -> str:
    """Strip full-line ``--`` comments and blank lines, rstrip, join with ``\\n``.

    Compares only the DDL, so the vendored copy's differing header/comment
    wording doesn't count as drift. The home-infra check does exactly the same.
    """
    lines = []
    for line in s.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines)


def test_pg_schema_matches_the_pinned_checksum():
    ddl = open(SCHEMA_FILE, encoding="utf-8").read()
    digest = hashlib.sha256(_normalise_sql(ddl).encode()).hexdigest()
    assert digest == SCHEMA_SHA256, (
        "pg schema DDL changed. If deliberate, apply the identical change to the "
        "vendored copy (luke/deployment/claude-chats/init.sql in home-infra) and "
        "update both pinned hashes (here and in that repo's "
        f"scripts/check-convmem-pg-schema.sh) to this value: {digest}"
    )


if __name__ == "__main__":
    test_pg_schema_matches_the_pinned_checksum()
    print("ok: pg schema matches the pinned checksum", SCHEMA_SHA256)
