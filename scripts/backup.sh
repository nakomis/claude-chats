#!/usr/bin/env bash
# Dump the claude_chats database, excluding the embedding vectors.
#
# Usage: scripts/backup.sh [--s3 | --local [file-or-directory]]
#
#   --s3     (default) upload the dump to s3://$CLAUDE_CHATS_BACKUP_BUCKET/$CLAUDE_CHATS_BACKUP_PREFIX/
#            using the $BACKUP_AWS_PROFILE profile. The default because the
#            installed com.nakomis.claude-chats-backup LaunchAgents run this
#            script with no arguments, and must keep backing up offsite.
#   --local  write the dump to disk, default ~/.claude-chats/backups/, and
#            keep only the newest $CLAUDE_CHATS_BACKUP_KEEP (default 14)
#            claude_chats_*.sql.gz there. The single-machine edition's
#            `install.sh --backup` always passes this. A bare path with no
#            flag also means --local.
#
# CLAUDE_CHATS_BACKUP_DEST=local|s3 overrides the default when no flag is given.
#
# Safe to run while the database is live — pg_dump takes a consistent snapshot.
# Dumps *through the container* (docker exec): the host need not have a
# Postgres client at all, only the image does.
set -euo pipefail

DEST="${CLAUDE_CHATS_BACKUP_DEST:-s3}"
DEST_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) DEST="local"; shift
                 if [[ $# -gt 0 && "$1" != --* ]]; then DEST_ARG="$1"; shift; fi ;;
        --s3)    DEST="s3"; shift ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --*)     echo "[backup] unknown option '$1'" >&2; exit 2 ;;
        *)       DEST="local"; DEST_ARG="$1"; shift ;;   # a bare path, as install.sh used to pass
    esac
done
[[ "$DEST" == "local" || "$DEST" == "s3" ]] || {
    echo "[backup] CLAUDE_CHATS_BACKUP_DEST must be 'local' or 's3' (got '$DEST')" >&2; exit 2; }

CONTAINER="${CLAUDE_CHATS_CONTAINER:-claude-chats-db}"
KEEP="${CLAUDE_CHATS_BACKUP_KEEP:-14}"
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || { echo "[backup] CLAUDE_CHATS_BACKUP_KEEP must be a positive integer" >&2; exit 2; }
DB_NAME="claude_chats"
DB_USER="claude"

BUCKET="${CLAUDE_CHATS_BACKUP_BUCKET:-nakomis-taiga-backups}"
PREFIX="${CLAUDE_CHATS_BACKUP_PREFIX:-conversation-memory}"
BACKUP_AWS_PROFILE="${BACKUP_AWS_PROFILE:-claude-chats-backup}"
REGION="${CLAUDE_CHATS_BACKUP_REGION:-eu-west-2}"

# launchd runs this with a minimal PATH that lacks Homebrew and Docker's
# binaries, so fall back to where they usually live rather than failing.
DOCKER="$(command -v docker || echo /usr/local/bin/docker)"
AWS="$(command -v aws || echo /opt/homebrew/bin/aws)"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILENAME="claude_chats_${TIMESTAMP}.sql.gz"

PRUNE_DIR=""
if [[ "$DEST" == "local" ]]; then
    DEST_ARG="${DEST_ARG:-${HOME}/.claude-chats/backups}"
    if [[ -d "$DEST_ARG" || "$DEST_ARG" != *.sql.gz ]]; then
        mkdir -p "$DEST_ARG"
        OUTFILE="${DEST_ARG%/}/${FILENAME}"
        # Only a directory of our own timestamped dumps is pruned; a dump
        # written to a named file is the caller's to keep.
        PRUNE_DIR="${DEST_ARG%/}"
    else
        mkdir -p "$(dirname "$DEST_ARG")"
        OUTFILE="$DEST_ARG"
    fi
else
    # macOS mktemp requires X's at the end — use a tmp dir, then name the file inside it
    TMPDIR_BACKUP="$(mktemp -d /tmp/claude-chats-backup-XXXXXX)"
    trap 'rm -rf "$TMPDIR_BACKUP"' EXIT
    OUTFILE="${TMPDIR_BACKUP}/${FILENAME}"
fi

# Fail early rather than writing or uploading an empty or partial dump.
if ! "$DOCKER" ps --filter "name=^/${CONTAINER}$" --filter "status=running" \
        --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[backup] ERROR: container '$CONTAINER' is not running — aborting" >&2
    exit 1
fi

# The messages.embedding column is deliberately excluded. The vectors are
# incompressible floats that dominate the dump (~144 MB of ~210 MB when this
# was measured), and they are fully regenerable from message content —
# restore.sh repopulates them via backfill-embeddings and rebuilds the ivfflat
# index. pg_dump cannot exclude a single column, so we:
#   1. dump schema + all data EXCEPT the messages rows (and the regenerable
#      bakeoff tables, which are normally absent — the excludes are no-ops then);
#   2. append the messages rows via an explicit column list.
# The appended COPY lands after pg_dump's CREATE INDEX statements, so a plain
# `psql < dump` restores cleanly with embeddings left NULL, ready for backfill.
#
# The column list is read from the database rather than written out, so a
# column added to init.sql is carried without anyone remembering to add it here
# (a fixed list once silently dropped author, tool_name and model). Left out:
#   * embedding, for the reason above;
#   * generated columns (content_tsv), which are recomputed on insert;
#   * embedder, embedder_board, embedder_fw, which describe the vector — the
#     restore's backfill makes a new one, and a tag left behind would claim it
#     came from wherever the old one did.
MSG_COLS="$("$DOCKER" exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "
    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'messages'
      AND is_generated = 'NEVER'
      AND column_name NOT IN ('embedding', 'embedder', 'embedder_board', 'embedder_fw')")"
[[ -n "$MSG_COLS" ]] || { echo "[backup] ERROR: could not read the messages columns" >&2; exit 1; }

echo "[backup] dumping ${DB_NAME} (via ${CONTAINER}, embedding vectors excluded) → ${OUTFILE}"
{
    "$DOCKER" exec "$CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" \
        --exclude-table='message_embeddings' \
        --exclude-table='embedding_perf' \
        --exclude-table-data='messages'
    printf '\n--\n-- messages data (embedding column excluded; repopulated by backfill-embeddings)\n--\n'
    printf 'COPY public.messages (%s) FROM stdin;\n' "$MSG_COLS"
    "$DOCKER" exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
        -c "COPY (SELECT ${MSG_COLS} FROM messages) TO STDOUT"
    printf '\\.\n'
} | gzip -9 > "$OUTFILE"

# Sanity check: a valid gzipped SQL dump is never this small.
BYTES="$(wc -c < "$OUTFILE" | tr -d ' ')"
if [[ "$BYTES" -lt 1000 ]]; then
    echo "[backup] ERROR: dump is only ${BYTES} bytes — likely failed, discarding" >&2
    rm -f "$OUTFILE"
    exit 1
fi
SIZE="$(du -h "$OUTFILE" | cut -f1)"

if [[ "$DEST" == "local" ]]; then
    echo "[backup] done: ${OUTFILE} (${SIZE})"
    if [[ -n "$PRUNE_DIR" ]]; then
        # Newest first by name (the timestamp sorts), so the one just written
        # is always kept. No mapfile/xargs -d: macOS ships bash 3.2 and BSD xargs.
        n=0
        while IFS= read -r old; do
            n=$((n + 1))
            if [[ $n -gt $KEEP ]]; then
                rm -f -- "$old"
                echo "[backup] pruned $(basename "$old") (keeping the newest ${KEEP})"
            fi
        done < <(ls -1 "$PRUNE_DIR"/claude_chats_*.sql.gz 2>/dev/null | sort -r)
    fi
    exit 0
fi

S3_KEY="${PREFIX}/${FILENAME}"
echo "[backup] uploading → s3://${BUCKET}/${S3_KEY}"
"$AWS" s3 cp "$OUTFILE" "s3://${BUCKET}/${S3_KEY}" \
    --profile "$BACKUP_AWS_PROFILE" \
    --region "$REGION" \
    --no-progress

echo "[backup] done: s3://${BUCKET}/${S3_KEY} (${SIZE})"
