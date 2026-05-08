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
docker exec "$DB_CONTAINER" psql -U claude -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec "$DB_CONTAINER" psql -U claude -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER claude;"
docker exec "$DB_CONTAINER" psql -U claude -d postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "[restore] loading dump"
gunzip -c "$TMPFILE" | psql "$DB_URL"

echo "[restore] verifying"
psql "$DB_URL" -c '\dt'
psql "$DB_URL" -c 'SELECT COUNT(*) AS conversations FROM conversations;'
psql "$DB_URL" -c 'SELECT COUNT(*) AS messages FROM messages;'

echo "[restore] complete"
