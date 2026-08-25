"""
Replay the hook's fallback file back into the SQLite outbox.

Both capture modes fall back to appending newline-delimited JSON when their
primary write fails — a corrupt outbox, a full disk, a Postgres that is down in
direct mode. That file is the last line of defence in a design whose whole point
is that capture cannot fail.

This drains it back into the outbox once the problem is fixed. It is idempotent:
rows go in with INSERT OR IGNORE against the UNIQUE message_uuid, so replaying
twice is harmless, and rows already drained are suppressed by their tombstones.

The outbox is the destination in *both* modes, even direct — it is simply the
staging table this recovery path already knows how to write. Run the drainer
afterwards to land the rows in Postgres; `install.sh --replay-fallback` does
both for you.

    replay-fallback            # replay, then archive the file
    replay-fallback --dry-run  # just report what it would do
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from hook.record import FALLBACK_PATH, OUTBOX_PATH, SCHEMA

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
