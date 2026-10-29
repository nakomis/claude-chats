#!/usr/bin/env bash
# Back up the claude_chats database to local disk: the single-machine
# edition's nightly (CHATS-41), scheduled by localhost/install.sh.
#
# Usage: scripts/backup.sh [DIR]
#
# DIR (default $CLAUDE_CHATS_BACKUP_DIR, else ~/.claude-chats/backups) gets:
#
#   archive/messages/  one file per complete month, written once by
#                      convmem-archive.sh and never pruned
#   nightly/           claude_chats_<timestamp>.sql.gz: the schema, every
#                      other table whole (conversations, Claude Facts), and
#                      the messages not yet archived; the newest
#                      $CLAUDE_CHATS_BACKUP_KEEP (default 14) are kept
#
# Restore with scripts/restore.sh (or ./install.sh --restore): the newest
# nightly plus every archived month before its cutoff.
#
# This is the same monthly-archive scheme Luke runs (HOME-458), from the same
# scripts/convmem-archive.sh, pointed at a directory instead of S3. Luke's
# nightly wrapper lives in home-infra, because its alerting is Luke's own.
#
# Embedding vectors are included, as on Luke: a restore is then minutes, not
# the hours a re-embed of every message would take. Through `docker exec`
# into claude-chats-db, so the host needs no Postgres client.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLD_AGENT="${HOME}/Library/LaunchAgents/com.nakomis.claude-chats-backup.plist"

die() { echo "[backup] ERROR: $*" >&2; exit 1; }
say() { echo "[backup] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# The Mac-to-S3 LaunchAgent (scripts/setup-backup.sh, retired in CHATS-41) ran
# `backup.sh --s3` every night. Luke backs conversation-memory up offsite now,
# so that path is gone, but a machine still carrying the agent should hear
# about it rather than quietly stop backing up: refuse, say why, and on a Mac
# put it in Notification Centre too, since the agent's log is rarely read.
retired() {
    local msg="$1"
    echo "[backup] REFUSED: ${msg}" >&2
    if [[ "$(uname -s)" == "Darwin" && "${CLAUDE_CHATS_BACKUP_NOTIFY:-1}" != 0 ]] \
            && command -v osascript >/dev/null; then
        osascript -e "display notification \"${msg//\"/}\" with title \"claude-chats backup\"" \
            >/dev/null 2>&1 || true
    fi
    exit 3
}
old_agent_message="the com.nakomis.claude-chats-backup LaunchAgent (Mac to S3) is retired: \
Luke backs conversation-memory up offsite now. Remove it with \
'launchctl bootout gui/\$(id -u)/com.nakomis.claude-chats-backup; rm ${OLD_AGENT}'. \
The single-machine edition's own nightly is installed by localhost/install.sh."

DIR="${CLAUDE_CHATS_BACKUP_DIR:-${HOME}/.claude-chats/backups}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --s3)      retired "$old_agent_message" ;;
        --local)   shift ;;   # what install.sh passed before CHATS-41; local is all there is
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --*)       die "unknown option '$1'" ;;
        *)         DIR="$1"; shift ;;
    esac
done
[[ -f "$OLD_AGENT" ]] && retired "$old_agent_message"
[[ "$DIR" == *.sql.gz ]] && die "give a directory, not a file: backups are a directory of months and nightlies now"

KEEP="${CLAUDE_CHATS_BACKUP_KEEP:-14}"
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "CLAUDE_CHATS_BACKUP_KEEP must be a positive integer"

# launchd and systemd run this with a minimal PATH, which lacks Docker's
# usual homes on a Mac.
export PATH="${PATH}:/usr/local/bin:/opt/homebrew/bin:${HOME}/.docker/bin"

export CONVMEM_TARGET=local
export CONVMEM_ARCHIVE_DIR="${DIR%/}/archive"
ARCHIVER="${SCRIPTS}/convmem-archive.sh"
NIGHTLY_DIR="${DIR%/}/nightly"
mkdir -p "$NIGHTLY_DIR" "$CONVMEM_ARCHIVE_DIR"

# The nightly is written beside its final name and renamed into place only
# once it has passed every check, so a failed run never leaves a dump that
# looks finished.
OUT="${NIGHTLY_DIR}/claude_chats_$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
PART="${OUT}.partial"
trap 'rm -f "$PART"' EXIT

say "starting conversation-memory backup to ${DIR}"

# Archive newly complete months first, so the nightly is as small as the archive
# allows. Not fatal, exactly as on Luke: a failed archive leaves the cutoff
# where it was and the nightly below covers more, never less.
if ! "$ARCHIVER" archive; then
    echo "[backup] WARNING: archiving old months failed; this nightly covers from $("$ARCHIVER" cutoff 2>/dev/null || echo "an unknown cutoff")" >&2
fi

EXPECTED_ROWS="$("$ARCHIVER" expect)"
say "window holds ${EXPECTED_ROWS} messages"

"$ARCHIVER" dump | gzip >"$PART"

# A partial stream still compresses; the floor is per row, as on Luke, because
# the window is a few rows on the 1st of the month and a month's worth by the
# 31st. A truncated gzip is the case no floor can see.
SIZE="$(wc -c <"$PART" | tr -d ' ')"
FLOOR=$((EXPECTED_ROWS * ${CONVMEM_MIN_BYTES_PER_ROW:-1024}))
[[ "$SIZE" -ge "$FLOOR" ]] \
    || die "the dump is only ${SIZE} bytes for ${EXPECTED_ROWS} messages (floor ${FLOOR}): treating it as failed"
gzip -t "$PART" || die "the dump is not a complete gzip stream"
mv -f "$PART" "$OUT"

# Newest first by name (the timestamp sorts), so the one just written is always
# kept. No mapfile: macOS ships bash 3.2.
n=0
while IFS= read -r old; do
    n=$((n + 1))
    if [[ $n -gt $KEEP ]]; then
        rm -f -- "$old"
        say "pruned $(basename "$old") (keeping the newest ${KEEP})"
    fi
done < <(ls -1 "$NIGHTLY_DIR"/claude_chats_*.sql.gz 2>/dev/null | sort -r)

say "backup finished: ${OUT} (${SIZE} bytes); $(ls -1 "${CONVMEM_ARCHIVE_DIR}/messages" 2>/dev/null | wc -l | tr -d ' ') archived month(s)"
