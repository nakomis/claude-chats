#!/usr/bin/env python3
"""Recover messages sent mid-turn that never reached the database (HOME-411).

A message sent while Claude was still working is recorded in the transcript as
a ``queued_command`` attachment, not a message, and the hook ignored it: the
text and any images were dropped. They are still in the transcripts, so this
reads them back with the hook's own code (one implementation, not two) and
queues them in the local outbox. The forwarder delivers them from there like
any other message, and their images are staged for flush-images to archive.

Only the mid-turn messages are queued. Replaying whole transcripts would work
too, since nothing downstream accepts a duplicate, but every message whose
outbox tombstone had gone would be sent and embedded again for nothing.

Each Mac holds its own transcripts, so run it on every one:

    uv run --project hook python scripts/backfill-mid-turn.py           # dry run
    uv run --project hook python scripts/backfill-mid-turn.py --apply

Safe to run more than once: the outbox ignores a message_uuid it already has,
images are deduplicated by hash, and the worker ignores rows it already holds.
"""

import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "hook"))

import hook.record as record  # noqa: E402
from hook.images import stage_images  # noqa: E402
from hook.record import _ai_title, _session_name, build_records  # noqa: E402

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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


def _last(entries: list[dict], key: str) -> str:
    """The session's latest value of ``key`` — what the hook's payload would carry."""
    for entry in reversed(entries):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--projects",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code projects directory (default: %(default)s)",
    )
    ap.add_argument("--apply", action="store_true",
                    help="write to the outbox and stage images (default: dry run)")
    args = ap.parse_args()

    sessions = messages_found = images_staged = 0
    for path in sorted(glob.glob(os.path.join(args.projects, "*", "*.jsonl"))):
        session_id = os.path.basename(path)[: -len(".jsonl")]
        if not _UUID_RE.match(session_id):
            continue
        entries = _entries(path)
        name = _session_name(entries, path, session_id)
        ai_title = _ai_title(entries)
        records, messages = build_records(
            entries, session_id, _last(entries, "cwd"), _last(entries, "gitBranch"),
            name, ai_title,
        )
        mid_turn = [m for m in messages if m.get("midTurn")]
        uuids = {m.get("uuid") for m in mid_turn}
        records = [r for r in records if r["message_uuid"] in uuids]
        if not records:
            continue

        sessions += 1
        messages_found += len(records)
        print(f"{session_id}  {len(records):3d} message(s)  {name or ai_title or ''}")
        if not args.apply:
            continue
        os.makedirs(os.path.dirname(record.OUTBOX_PATH), exist_ok=True)
        record._append_to_outbox(records, session_id, name, ai_title)
        images_staged += stage_images(
            mid_turn, {}, session_id, _last(entries, "cwd"), _last(entries, "gitBranch"),
            text_by_uuid={
                m.get("uuid") or "": record._extract_text(m["message"]["content"])
                for m in mid_turn
            },
        )

    verb = "queued" if args.apply else "would queue"
    summary = f"{verb} {messages_found} mid-turn message(s) from {sessions} session(s)"
    if args.apply:
        summary += f"; staged {images_staged} image(s)"
    print(summary, file=sys.stderr)


if __name__ == "__main__":
    main()
