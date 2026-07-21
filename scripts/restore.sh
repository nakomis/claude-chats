#!/usr/bin/env bash
# Restores the claude_chats database from an S3 backup.
# Usage: ./restore.sh [s3-key]
#   s3-key: full object key, e.g. conversation-memory/claude_chats_20260508T030000Z.sql.gz
#           Omit to list available backups and pick interactively.
#
# WARNING: This drops and recreates the claude_chats database.
#          Run backup.sh first if you have local changes worth keeping.
set -euo pipefail

BUCKET="nakomis-taiga-backups"
PREFIX="conversation-memory"
BACKUP_AWS_PROFILE="${BACKUP_AWS_PROFILE:-claude-chats-backup}"
REGION="eu-west-2"

DB_CONTAINER="claude-chats-db"
DB_NAME="claude_chats"
DB_URL="postgresql://claude:claude@localhost:5433/${DB_NAME}"

list_backups() {
    aws s3 ls "s3://${BUCKET}/${PREFIX}/" \
        --profile "$BACKUP_AWS_PROFILE" \
        --region "$REGION" \
        | awk '{print $NF}' \
        | sort -r
}

if [[ $# -eq 0 ]]; then
    echo "Available backups (newest first):"
    mapfile -t KEYS < <(list_backups)
    if [[ ${#KEYS[@]} -eq 0 ]]; then
        echo "No backups found in s3://${BUCKET}/${PREFIX}/"
        exit 1
    fi
    for i in "${!KEYS[@]}"; do
        printf "  %2d) %s\n" "$((i+1))" "${KEYS[$i]}"
    done
    read -rp "Select backup number [1]: " CHOICE
    CHOICE="${CHOICE:-1}"
    S3_KEY="${PREFIX}/${KEYS[$((CHOICE-1))]}"
else
    S3_KEY="$1"
fi

echo
echo "Restoring from: s3://${BUCKET}/${S3_KEY}"
read -rp "This will WIPE the local ${DB_NAME} database. Type 'yes' to continue: " CONFIRM
[[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 1; }

TMPFILE="$(mktemp /tmp/claude-chats-restore-XXXXXX.sql.gz)"
cleanup() { rm -f "$TMPFILE"; }
trap cleanup EXIT

echo "[restore] downloading $S3_KEY"
aws s3 cp "s3://${BUCKET}/${S3_KEY}" "$TMPFILE" \
    --profile "$BACKUP_AWS_PROFILE" \
    --region "$REGION" \
    --no-progress

echo "[restore] dropping and recreating database"
# psql is routed through the container: the host has no postgres client, only
# the Docker image does. The vector extension is created by the dump itself
# (init.sql's CREATE EXTENSION is included), so we don't pre-create it here.
docker exec "$DB_CONTAINER" psql -U claude -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec "$DB_CONTAINER" psql -U claude -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER claude;"

echo "[restore] loading dump (embeddings excluded — repopulated below)"
gunzip -c "$TMPFILE" | docker exec -i "$DB_CONTAINER" psql -q -U claude -d "$DB_NAME"

echo "[restore] verifying row counts"
docker exec "$DB_CONTAINER" psql -U claude -d "$DB_NAME" -c '\dt'
docker exec "$DB_CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS conversations FROM conversations;'
docker exec "$DB_CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS messages, COUNT(embedding) AS with_embedding FROM messages;'

# The dump carries message content but not the embedding vectors. Regenerate
# them and rebuild the ivfflat index. Runs on the host via uv so it can reach
# both Postgres (localhost:5433) and the embedding provider (e.g. Ollama on
# localhost:11434), using the same hook.embed helper the live recorder uses.
echo "[restore] backfilling embeddings (this is the slow part — ~15-20 min for a full history)"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_CHATS_DB_URL="$DB_URL" uv run --project "${REPO_DIR}/hook" backfill-embeddings

echo "[restore] verifying embeddings"
docker exec "$DB_CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS messages, COUNT(embedding) AS with_embedding FROM messages;'

echo "[restore] complete"
