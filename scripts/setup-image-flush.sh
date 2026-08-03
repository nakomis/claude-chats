#!/usr/bin/env bash
# One-shot setup for the image-flush LaunchAgent (HOME-298).
#
# The record-conversation hook stages images sent to Claude into a LOCAL
# directory (~/.claude-chats/images). This agent moves them onto the NAS at
# /Volumes/share/claude-chats/images every 15 minutes.
#
# Why the split: the share is an SMB mount over two wireless hops — a real flush
# of 18 MB measured ~60s, and a write to a hung mount blocks uninterruptibly.
# The hook has a 120s timeout, so doing this inline would risk stalling Claude
# and would reintroduce the "capture can fail" bug record.py exists to prevent.
# Staging is fast and local; only this half is allowed to fail.
#
# Safe to re-run. Requires: uv on PATH.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.nakomis.claude-chats-flush-images"
PLIST_SRC="${REPO}/scripts/${LABEL}.plist"
LAUNCHAGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LAUNCHAGENTS_DIR}/${LABEL}.plist"

echo "=== claude-chats image flush setup ==="

# ── 1. Locate uv ──────────────────────────────────────────────────────────────
# Absolute path, not `/usr/bin/env uv`: launchd runs with a minimal PATH that
# excludes /opt/homebrew/bin, so env would fail to find it and the agent would
# fail silently every 15 minutes — the worst kind of broken.
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
    echo "  uv not found on PATH — install it first (brew install uv)" >&2
    exit 1
fi
echo "[1/3] uv: ${UV_BIN}"

# ── 2. Install the agent ──────────────────────────────────────────────────────
echo "[2/3] Installing LaunchAgent..."
mkdir -p "$LAUNCHAGENTS_DIR"
sed -e "s|REPO_PATH|${REPO}|g" -e "s|UV_PATH|${UV_BIN}|g" "$PLIST_SRC" > "$PLIST_DEST"

# Fail loudly rather than installing a plist with placeholders still in it.
if grep -q 'REPO_PATH\|UV_PATH' "$PLIST_DEST"; then
    echo "  placeholders left unsubstituted — template changed shape?" >&2
    rm -f "$PLIST_DEST"
    exit 1
fi
plutil -lint "$PLIST_DEST" >/dev/null

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
echo "  Installed: ${PLIST_DEST}"

# ── 3. Smoke test ─────────────────────────────────────────────────────────────
# RunAtLoad has already fired it; this confirms the command itself works when
# invoked directly, so a failure is attributable to the agent or to flush-images
# rather than ambiguous between them.
echo "[3/3] Running a flush now..."
"$UV_BIN" run --project "${REPO}/hook" flush-images

echo
echo "=== Setup complete ==="
echo "Flushes every 15 minutes. Log: /tmp/claude-chats-flush-images.log"
echo "Staging:     ~/.claude-chats/images"
echo "Destination: /Volumes/share/claude-chats/images"
echo
echo "If the share is unmounted, flush-images says so and leaves everything"
echo "staged — nothing is lost, it drains on the next run."
