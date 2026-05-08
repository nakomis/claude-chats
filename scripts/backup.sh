#!/usr/bin/env bash
# Dumps the claude_chats Postgres database and uploads it to S3.
# Requires AWS credentials in the [claude-chats-backup] profile (~/.aws/credentials).
# Safe to run while the DB is live — pg_dump takes a consistent snapshot.
set -euo pipefail

BUCKET="nakomis-taiga-backups"
PREFIX="conversation-memory"
BACKUP_AWS_PROFILE="${BACKUP_AWS_PROFILE:-claude-chats-backup}"
REGION="eu-west-2"

DB_URL="postgresql://claude:claude@localhost:5433/claude_chats"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILENAME="claude_chats_${TIMESTAMP}.sql.gz"
# macOS mktemp requires X's at the end — use a tmp dir, then name the file inside it
TMPDIR_BACKUP="$(mktemp -d /tmp/claude-chats-backup-XXXXXX)"
TMPFILE="${TMPDIR_BACKUP}/${FILENAME}"

cleanup() { rm -rf "$TMPDIR_BACKUP"; }
trap cleanup EXIT

echo "[backup] dumping claude_chats → $TMPFILE"
pg_dump "$DB_URL" | gzip -9 > "$TMPFILE"
SIZE="$(du -sh "$TMPFILE" | cut -f1)"
echo "[backup] dump complete: $SIZE compressed"

S3_KEY="${PREFIX}/${FILENAME}"
echo "[backup] uploading → s3://${BUCKET}/${S3_KEY}"
aws s3 cp "$TMPFILE" "s3://${BUCKET}/${S3_KEY}" \
    --profile "$BACKUP_AWS_PROFILE" \
    --region "$REGION" \
    --no-progress

echo "[backup] done: s3://${BUCKET}/${S3_KEY} ($SIZE)"
