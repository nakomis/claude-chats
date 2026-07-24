#!/usr/bin/env python3
"""
Replay the hook's fallback file back into the SQLite outbox.

The Stop hook writes to a local SQLite outbox. If SQLite itself is unavailable
— a corrupt file, a full disk, a permissions mishap — it falls back to
appending newline-delimited JSON, so the conversation is captured even when the
primary path is broken. That fallback is the last line of defence in a design
whose whole point is that capture cannot fail.

This script drains that file back into the outbox once the problem is fixed.
It is idempotent: rows are inserted with INSERT OR IGNORE against the UNIQUE
message_uuid, so replaying twice is harmless, and rows already forwarded are
suppressed by their tombstones.

    python3 scripts/replay-fallback.py            # replay, then archive the file
    python3 scripts/replay-fallback.py --dry-run  # just report what it would do
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Import the paths and schema from the hook itself rather than restating them:
# the outbox we are recovering into may not exist at all (the case this script
# exists for is a corrupt or deleted database), so we need to be able to create
# it, and a second copy of the schema would inevitably drift.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook")
)
from hook.record import FALLBACK_PATH, OUTBOX_PATH, SCHEMA  # noqa: E402

FIELDS = (
    "message_uuid", "session_id", "project_path", "git_branch",
    "conversation_name", "host", "role", "content", "sequence_num", "created_at",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--fallback", default=FALLBACK_PATH, help="fallback file to replay")
    args = ap.parse_args()

    if not os.path.exists(args.fallback):
        print(f"nothing to replay: {args.fallback} does not exist")
        return 0

    records, malformed = [], 0
    with open(args.fallback) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not all(f in rec for f in FIELDS):
                malformed += 1
                continue
            records.append({f: rec[f] for f in FIELDS})

    print(f"{len(records)} record(s) to replay from {args.fallback}")
    if malformed:
        # Report rather than discard silently — a partially written final line
        # is expected after a crash, anything more is worth a look.
        print(f"WARNING: {malformed} malformed line(s) skipped", file=sys.stderr)

    if args.dry_run or not records:
        return 0

    os.makedirs(os.path.dirname(OUTBOX_PATH), exist_ok=True)
    conn = sqlite3.connect(OUTBOX_PATH, timeout=10)
    try:
        # The outbox may be missing entirely — that is the whole point of the
        # fallback — so create it before inserting.
        conn.executescript(SCHEMA)
        with conn:
            before = conn.total_changes
            conn.executemany(
                f"""
                INSERT OR IGNORE INTO outbox ({", ".join(FIELDS)})
                VALUES ({", ".join(":" + f for f in FIELDS)})
                """,
                records,
            )
            inserted = conn.total_changes - before
    finally:
        conn.close()

    print(f"inserted {inserted} new record(s) ({len(records) - inserted} already present)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = f"{args.fallback}.{stamp}.replayed"
    os.rename(args.fallback, archived)
    print(f"archived original to {archived}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
