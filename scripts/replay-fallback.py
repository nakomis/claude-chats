#!/usr/bin/env python3
"""
Replay the hook's fallback file back into the SQLite outbox.

Kept so existing runbooks still work; the implementation is the hook's own
`replay-fallback` command (hook/hook/replay_fallback.py), which both editions
share. Prefer:

    uv run --project hook replay-fallback            # replay, then archive the file
    uv run --project hook replay-fallback --dry-run  # just report what it would do
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook")
)
from hook.replay_fallback import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
