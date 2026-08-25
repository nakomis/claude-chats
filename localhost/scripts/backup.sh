#!/usr/bin/env bash
# Dump the claude_chats database to a local file.
#
# Usage: scripts/backup.sh [destination-file-or-directory]
#        Default destination: ~/.claude-chats/backups/
#
# Safe to run while the database is live — pg_dump takes a consistent snapshot.
#
# The distributed edition's equivalent uploads to S3; this one writes to disk
# and leaves getting it somewhere safe up to you. Everything else is the same,
# including the trick below.
set -euo pipefail

CONTAINER="${CLAUDE_CHATS_CONTAINER:-claude-chats-db}"
DB_NAME="claude_chats"
DB_USER="claude"

DEST_ARG="${1:-${HOME}/.claude-chats/backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ -d "$DEST_ARG" || "$DEST_ARG" != *.sql.gz ]]; then
    mkdir -p "$DEST_ARG"
    OUTFILE="${DEST_ARG%/}/claude_chats_${TIMESTAMP}.sql.gz"
else
    mkdir -p "$(dirname "$DEST_ARG")"
    OUTFILE="$DEST_ARG"
fi

# Fail early rather than writing an empty or partial dump.
if ! docker ps --filter "name=^/${CONTAINER}$" --filter "status=running" \
        --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[backup] ERROR: container '$CONTAINER' is not running — aborting" >&2
    exit 1
fi

# The messages.embedding column is deliberately excluded. The vectors are
# incompressible floats that dominate the dump, and they are fully regenerable
# from message content — restore.sh repopulates them via backfill-embeddings and
# rebuilds the ivfflat index. pg_dump cannot exclude a single column, so we:
#   1. dump schema + all data EXCEPT the messages rows;
#   2. append the messages rows via an explicit column list that omits both
#      `embedding` and the generated `content_tsv` column (recomputed on insert).
# The appended COPY lands after pg_dump's CREATE INDEX statements, so a plain
# `psql < dump` restores cleanly with embeddings left NULL, ready for backfill.
echo "[backup] dumping ${DB_NAME} (embedding vectors excluded) → ${OUTFILE}"
MSG_COLS="id, conversation_id, message_uuid, role, content, created_at, sequence_num"
{
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" \
        --exclude-table-data='messages'
    printf '\n--\n-- messages data (embedding column excluded; repopulated by backfill-embeddings)\n--\n'
    printf 'COPY public.messages (%s) FROM stdin;\n' "$MSG_COLS"
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
        -c "COPY (SELECT ${MSG_COLS} FROM messages) TO STDOUT"
    printf '\\.\n'
} | gzip -9 > "$OUTFILE"

# Sanity check: a valid gzipped SQL dump is never this small.
BYTES="$(wc -c < "$OUTFILE" | tr -d ' ')"
if [[ "$BYTES" -lt 1000 ]]; then
    echo "[backup] ERROR: dump is only ${BYTES} bytes — likely failed, removing" >&2
    rm -f "$OUTFILE"
    exit 1
fi

echo "[backup] done: ${OUTFILE} ($(du -h "$OUTFILE" | cut -f1))"
