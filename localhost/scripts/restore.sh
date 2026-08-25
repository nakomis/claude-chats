#!/usr/bin/env bash
# Restore the claude_chats database from a local backup file.
#
# Usage: scripts/restore.sh [file.sql.gz]
#        Omit the file to pick interactively from ~/.claude-chats/backups/.
#
# WARNING: this drops and recreates the claude_chats database.
#          Run backup.sh first if the current contents matter.
set -euo pipefail

BACKUP_DIR="${CLAUDE_CHATS_BACKUP_DIR:-${HOME}/.claude-chats/backups}"
CONTAINER="${CLAUDE_CHATS_CONTAINER:-claude-chats-db}"
DB_NAME="claude_chats"
DB_URL="postgresql://claude:claude@localhost:5433/${DB_NAME}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -ge 1 ]]; then
    DUMP="$1"
else
    echo "Available backups in ${BACKUP_DIR} (newest first):"
    # No mapfile: it is bash 4+, and macOS still ships bash 3.2.
    FILES=()
    while IFS= read -r f; do FILES+=("$f"); done < <(ls -1t "${BACKUP_DIR}"/*.sql.gz 2>/dev/null || true)
    if [[ ${#FILES[@]} -eq 0 ]]; then
        echo "No backups found in ${BACKUP_DIR}" >&2
        exit 1
    fi
    for i in "${!FILES[@]}"; do
        printf "  %2d) %s\n" "$((i+1))" "$(basename "${FILES[$i]}")"
    done
    read -rp "Select backup number [1]: " CHOICE
    CHOICE="${CHOICE:-1}"
    DUMP="${FILES[$((CHOICE-1))]}"
fi

[[ -f "$DUMP" ]] || { echo "No such file: $DUMP" >&2; exit 1; }

echo
echo "Restoring from: ${DUMP}"
read -rp "This will WIPE the local ${DB_NAME} database. Type 'yes' to continue: " CONFIRM
[[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 1; }

echo "[restore] dropping and recreating database"
# psql runs inside the container: the host has no postgres client, only the
# image does. The vector extension is created by the dump itself (init.sql's
# CREATE EXTENSION is included), so it is not pre-created here.
docker exec "$CONTAINER" psql -U claude -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec "$CONTAINER" psql -U claude -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER claude;"

echo "[restore] loading dump (embeddings excluded — repopulated below)"
gunzip -c "$DUMP" | docker exec -i "$CONTAINER" psql -q -U claude -d "$DB_NAME"

echo "[restore] row counts"
docker exec "$CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS conversations FROM conversations;'
docker exec "$CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS messages, COUNT(embedding) AS with_embedding FROM messages;'

# The dump carries message content but not the vectors. Regenerate them and
# rebuild the ivfflat index, using the same hook.embed helper the recorder uses.
echo "[restore] backfilling embeddings (the slow part — expect minutes, not seconds)"
CLAUDE_CHATS_DB_URL="$DB_URL" uv run --project "${REPO_DIR}/hook" backfill-embeddings

echo "[restore] verifying embeddings"
docker exec "$CONTAINER" psql -U claude -d "$DB_NAME" \
    -c 'SELECT COUNT(*) AS messages, COUNT(embedding) AS with_embedding FROM messages;'

echo "[restore] complete"
