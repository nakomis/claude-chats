#!/usr/bin/env bash
# Restore the claude_chats database from a backup made by backup.sh.
#
# Usage: scripts/restore.sh [--local] [file.sql.gz]
#        scripts/restore.sh --s3 [s3-key]
#
#   --local  (default) restore a file. Omit it to pick from
#            $CLAUDE_CHATS_BACKUP_DIR (default ~/.claude-chats/backups/).
#   --s3     download from s3://$CLAUDE_CHATS_BACKUP_BUCKET/ first. The key is
#            the full object key, e.g.
#            conversation-memory/claude_chats_20260508T030000Z.sql.gz;
#            omit it to pick from a listing.
#
# CLAUDE_CHATS_BACKUP_DEST=local|s3 sets the default when no flag is given.
#
# WARNING: this drops and recreates the claude_chats database.
#          Run backup.sh first if the current contents matter.
set -euo pipefail

SRC="${CLAUDE_CHATS_BACKUP_DEST:-local}"
ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) SRC="local"; shift ;;
        --s3)    SRC="s3"; shift ;;
        -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --*)     echo "[restore] unknown option '$1'" >&2; exit 2 ;;
        *)       ARG="$1"; shift ;;
    esac
done
[[ "$SRC" == "local" || "$SRC" == "s3" ]] || {
    echo "[restore] CLAUDE_CHATS_BACKUP_DEST must be 'local' or 's3' (got '$SRC')" >&2; exit 2; }

BACKUP_DIR="${CLAUDE_CHATS_BACKUP_DIR:-${HOME}/.claude-chats/backups}"
BUCKET="${CLAUDE_CHATS_BACKUP_BUCKET:-nakomis-taiga-backups}"
PREFIX="${CLAUDE_CHATS_BACKUP_PREFIX:-conversation-memory}"
BACKUP_AWS_PROFILE="${BACKUP_AWS_PROFILE:-claude-chats-backup}"
REGION="${CLAUDE_CHATS_BACKUP_REGION:-eu-west-2}"

CONTAINER="${CLAUDE_CHATS_CONTAINER:-claude-chats-db}"
DB_NAME="claude_chats"
DB_URL="postgresql://claude:claude@localhost:5433/${DB_NAME}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Number a list on stdout and read a choice; prints the chosen line.
# No mapfile: it is bash 4+, and macOS still ships bash 3.2.
pick() {
    local items=() line choice
    while IFS= read -r line; do [[ -n "$line" ]] && items+=("$line"); done
    if [[ ${#items[@]} -eq 0 ]]; then
        echo "No backups found." >&2
        exit 1
    fi
    for i in "${!items[@]}"; do
        printf "  %2d) %s\n" "$((i+1))" "$(basename "${items[$i]}")" >&2
    done
    read -rp "Select backup number [1]: " choice </dev/tty
    echo "${items[$(( ${choice:-1} - 1 ))]}"
}

if [[ "$SRC" == "local" ]]; then
    if [[ -n "$ARG" ]]; then
        DUMP="$ARG"
    else
        echo "Available backups in ${BACKUP_DIR} (newest first):"
        DUMP="$(ls -1t "${BACKUP_DIR}"/*.sql.gz 2>/dev/null | pick)"
    fi
    [[ -f "$DUMP" ]] || { echo "No such file: $DUMP" >&2; exit 1; }
    LABEL="$DUMP"
else
    if [[ -n "$ARG" ]]; then
        S3_KEY="$ARG"
    else
        echo "Available backups in s3://${BUCKET}/${PREFIX}/ (newest first):"
        S3_KEY="${PREFIX}/$(aws s3 ls "s3://${BUCKET}/${PREFIX}/" \
            --profile "$BACKUP_AWS_PROFILE" --region "$REGION" \
            | awk '{print $NF}' | sort -r | pick)"
    fi
    LABEL="s3://${BUCKET}/${S3_KEY}"
fi

echo
echo "Restoring from: ${LABEL}"
read -rp "This will WIPE the local ${DB_NAME} database. Type 'yes' to continue: " CONFIRM
[[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 1; }

if [[ "$SRC" == "s3" ]]; then
    DUMP="$(mktemp /tmp/claude-chats-restore-XXXXXX)"
    trap 'rm -f "$DUMP"' EXIT
    echo "[restore] downloading ${S3_KEY}"
    aws s3 cp "s3://${BUCKET}/${S3_KEY}" "$DUMP" \
        --profile "$BACKUP_AWS_PROFILE" \
        --region "$REGION" \
        --no-progress
fi

echo "[restore] dropping and recreating database"
# psql runs inside the container: the host has no postgres client, only the
# image does. The vector extension is created by the dump itself (init.sql's
# CREATE EXTENSION is included), so it is not pre-created here.
docker exec "$CONTAINER" psql -U claude -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec "$CONTAINER" psql -U claude -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER claude;"

echo "[restore] loading dump (embeddings excluded — repopulated below)"
gunzip -c "$DUMP" | docker exec -i "$CONTAINER" psql -q -U claude -d "$DB_NAME"

# A dump taken before a schema change lacks the newer columns; init.sql adds
# them, idempotently, exactly as it does on a live database.
echo "[restore] bringing the schema up to date"
docker exec -i "$CONTAINER" psql -q -v ON_ERROR_STOP=1 -U claude -d "$DB_NAME" \
    < "${REPO_DIR}/init.sql" >/dev/null

echo "[restore] row counts"
docker exec "$CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS conversations FROM conversations;'
docker exec "$CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS messages, COUNT(embedding) AS with_embedding FROM messages;'

# The dump carries message content but not the vectors. Regenerate them and
# rebuild the ivfflat index, using the same hook.embed helper the recorder
# uses. Runs on the host via uv so it can reach both Postgres (localhost:5433)
# and the embedding provider (e.g. Ollama on localhost:11434).
echo "[restore] backfilling embeddings (the slow part — expect minutes, not seconds)"
CLAUDE_CHATS_DB_URL="$DB_URL" uv run --project "${REPO_DIR}/hook" backfill-embeddings

echo "[restore] verifying embeddings"
docker exec "$CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS messages, COUNT(embedding) AS with_embedding FROM messages;'

echo "[restore] complete"
