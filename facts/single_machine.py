# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""One facts service run on a single machine (CHATS-39), as the launchd agent or systemd timer fires it.

    python -m facts.single_machine --env-file ~/.claude-chats/config.env [--limit N] [--dry-run]

Reads the settings localhost/install.sh recorded, fills in what the single-machine edition
always means (the one database, facts embedded through hook.embed exactly as queries are),
takes a lock so only one run happens at a time, then hands over to facts.service.

Never run from the Claude Code hook or the drain: extraction makes model calls, and with
the claude-code provider each one is a `claude -p` process.
"""

import argparse
import fcntl
import os
import sys
from pathlib import Path

DEFAULT_PROVIDER = "claude-code"


def single_machine_env(env):
    """Fill in the facts service's settings from install.sh's own names, without overriding any."""
    env.setdefault("FACTS_PROVIDER", DEFAULT_PROVIDER)
    if env.get("CLAUDE_CHATS_DB_URL"):
        env.setdefault("FACTS_DATABASE_URL", env["CLAUDE_CHATS_DB_URL"])
    env.setdefault("FACTS_EMBEDDER", "hook")
    # New conversations before any backfilled history (./install.sh --facts-backfill).
    if env.get("FACTS_INSTALLED_AT"):
        env.setdefault("FACTS_PRIORITY_SINCE", env["FACTS_INSTALLED_AT"])
    if env.get("CLAUDE_CHATS_DIMENSIONS"):
        env.setdefault("FACTS_EMBED_DIMS", env["CLAUDE_CHATS_DIMENSIONS"])
    return env


def take_lock(path):
    """An exclusive, non-blocking lock on path: the open file while held, None if another run has it.

    The database advisory lock in facts.service also serialises runs; this one is taken first
    so a second run never even starts model work, and it holds while Postgres is unreachable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(description="One facts run with the single-machine edition's settings.")
    ap.add_argument("--env-file", required=True, help="install.sh's config.env")
    ap.add_argument("--lock-file", help="default: facts.lock beside the env file")
    args, rest = ap.parse_known_args(argv)

    from hook.drain import load_env_file  # the drain's parser: the same file, read the same way

    try:
        load_env_file(args.env_file)
    except OSError as e:
        sys.exit(f"claude-chats facts: cannot read {args.env_file} ({e})")
    env = single_machine_env(os.environ)
    if env["FACTS_PROVIDER"] == "off":
        print("claude-chats facts: extraction is off (FACTS_PROVIDER=off in the config)", file=sys.stderr)
        return
    if env["FACTS_PROVIDER"] == "anthropic" and not env.get("ANTHROPIC_API_KEY"):
        # facts.service would otherwise look for the distributed edition's key in SSM.
        sys.exit("claude-chats facts: FACTS_PROVIDER=anthropic needs ANTHROPIC_API_KEY; re-run ./install.sh")

    lock = take_lock(Path(args.lock_file) if args.lock_file else Path(args.env_file).parent / "facts.lock")
    if lock is None:
        print("claude-chats facts: another run is still going; skipping this one", file=sys.stderr)
        return
    with lock:
        from facts import service

        service.main(rest)


if __name__ == "__main__":
    main()
