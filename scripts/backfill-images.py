#!/usr/bin/env python3
"""One-shot: stage images from every historic transcript (HOME-298).

The record-conversation hook already re-reads the *whole* transcript on each
run, so any session that is still being used backfills itself. This exists for
sessions that will never run again — the bulk of the archive.

Staging only. It never touches the share: `flush-images` (or its LaunchAgent)
delivers, refusing politely when the mount is absent. Dedupe is by sha256, so
running this repeatedly, or alongside the hook, costs nothing.

    python3 scripts/backfill-images.py            # stage everything
    python3 scripts/backfill-images.py --dry-run  # count only
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hook"))

from hook.images import STAGING_DIR, stage_images  # noqa: E402
from hook.record import _extract_text, _image_sources  # noqa: E402

PROJECTS = os.path.expanduser("~/.claude/projects")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be staged, write nothing")
    ap.add_argument("--projects", default=PROJECTS,
                    help=f"transcript root (default {PROJECTS})")
    args = ap.parse_args()

    transcripts = sorted(glob.glob(os.path.join(args.projects, "*", "*.jsonl")))
    if not transcripts:
        print(f"no transcripts under {args.projects}")
        return 0

    print(f"{len(transcripts)} transcript(s) under {args.projects}")
    print(f"staging to {STAGING_DIR}" if not args.dry_run else "dry run — writing nothing")

    total_images = total_staged = skipped = 0
    for path in transcripts:
        session_id = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, errors="replace") as fh:
                entries = [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            # A truncated or unreadable transcript must not stop the rest.
            skipped += 1
            continue

        messages = [
            e for e in entries
            if isinstance(e.get("message"), dict)
            and e["message"].get("role") in ("user", "assistant")
        ]
        # Only images Martin sent. Tool results carry image blocks too — Claude
        # reading a PNG puts one in the transcript — and archiving those would
        # copy Claude's own file reads into the archive.
        user_messages = [e for e in messages if e["message"].get("role") == "user"]
        n_images = sum(
            1
            for e in user_messages
            for b in (e["message"].get("content") or [])
            if isinstance(b, dict) and b.get("type") == "image"
        )
        total_images += n_images
        if not n_images:
            continue

        if args.dry_run:
            print(f"  {session_id[:8]}  {n_images:>3} image(s)")
            continue

        staged = stage_images(
            user_messages,
            _image_sources(entries),
            session_id,
            os.path.dirname(path),
            "",
            text_by_uuid={
                e.get("uuid") or "": _extract_text(e.get("message", {}).get("content", ""))
                for e in user_messages
            },
        )
        total_staged += staged
        if staged:
            print(f"  {session_id[:8]}  staged {staged} of {n_images}")

    print()
    print(f"image blocks found : {total_images}")
    if not args.dry_run:
        print(f"newly staged       : {total_staged}  "
              f"({total_images - total_staged} already known)")
    if skipped:
        print(f"unreadable         : {skipped} transcript(s)")
    print("\nRun `flush-images` (or wait for the agent) to deliver to the share.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
