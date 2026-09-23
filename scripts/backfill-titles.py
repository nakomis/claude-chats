#!/usr/bin/env python3
"""Recover session names and AI titles that never reached the database (HOME-391).

The hook looked for /rename in a transcript format Claude Code had stopped
writing, so every session renamed after the change reached Postgres with no
name. The names were on disk all along; this reads them back with the hook's
own extraction code (one implementation, not two) and prints SQL.

It prints rather than connects: the Macs have no route to Luke's Postgres, and
it has to run on every Mac that holds transcripts. Review the output, then:

    python3 scripts/backfill-titles.py > /tmp/titles.sql
    ssh luke 'docker exec -i claude-chats-db psql -U claude -d claude_chats \\
        -v ON_ERROR_STOP=1' < /tmp/titles.sql

Safe to run more than once, and in any order with HOME-393's name-guessing:

  * ``name`` is overwritten only where a human title exists and differs from
    the stored one — so a real name replaces a guess, never the reverse, and a
    session with no human title keeps whatever it has;
  * ``ai_title`` is set wherever the transcript has one.

Sessions not in the database are no-ops (the UPDATE matches nothing).
"""

import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "hook"))

from hook.record import _ai_title, _session_name  # noqa: E402

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _lit(value: str) -> str:
    """A Postgres string literal (standard_conforming_strings, the default)."""
    return "'" + value.replace("'", "''") + "'"


def _entries(path: str) -> list[dict]:
    out = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a half-written last line on a live session
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--projects",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code projects directory (default: %(default)s)",
    )
    args = ap.parse_args()

    named = titled = 0
    print("BEGIN;")
    for path in sorted(glob.glob(os.path.join(args.projects, "*", "*.jsonl"))):
        session_id = os.path.basename(path)[: -len(".jsonl")]
        if not _UUID_RE.match(session_id):
            continue
        entries = _entries(path)
        name = _session_name(entries, path, session_id)
        ai_title = _ai_title(entries)
        sid = _lit(session_id)
        if name:
            named += 1
            print(f"UPDATE conversations SET name = {_lit(name)} "
                  f"WHERE session_id = {sid} AND name IS DISTINCT FROM {_lit(name)};")
        if ai_title:
            titled += 1
            print(f"UPDATE conversations SET ai_title = {_lit(ai_title)} "
                  f"WHERE session_id = {sid} AND ai_title IS DISTINCT FROM {_lit(ai_title)};")
    print("COMMIT;")
    print(f"-- {named} session(s) with a human name, {titled} with an AI title",
          file=sys.stderr)


if __name__ == "__main__":
    main()
