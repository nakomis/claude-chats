#!/usr/bin/env bash
# Dumps the claude_chats Postgres database and uploads it to S3.
# Requires AWS credentials in the [claude-chats-backup] profile (~/.aws/credentials).
# Safe to run while the DB is live — pg_dump takes a consistent snapshot.
#
# Runs under launchd, which provides a minimal PATH that lacks Homebrew and
# Docker binaries. We therefore use absolute paths and dump *through the
# container* (docker exec) rather than relying on a host-side pg_dump, which
# does not exist on this machine outside the container.
set -euo pipefail

BUCKET="nakomis-taiga-backups"
PREFIX="conversation-memory"
BACKUP_AWS_PROFILE="${BACKUP_AWS_PROFILE:-claude-chats-backup}"
REGION="eu-west-2"

# Absolute paths — launchd's PATH does not include these locations.
DOCKER="/usr/local/bin/docker"
AWS="/opt/homebrew/bin/aws"
GZIP="/usr/bin/gzip"

CONTAINER="claude-chats-db"
DB_NAME="claude_chats"
DB_USER="claude"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILENAME="claude_chats_${TIMESTAMP}.sql.gz"
# macOS mktemp requires X's at the end — use a tmp dir, then name the file inside it
TMPDIR_BACKUP="$(mktemp -d /tmp/claude-chats-backup-XXXXXX)"
TMPFILE="${TMPDIR_BACKUP}/${FILENAME}"

cleanup() { rm -rf "$TMPDIR_BACKUP"; }
trap cleanup EXIT

# Fail loudly and early if the container is not running, rather than uploading
# an empty/partial dump.
if ! "$DOCKER" ps --filter "name=^/${CONTAINER}$" --filter "status=running" \
        --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[backup] ERROR: container '$CONTAINER' is not running — aborting" >&2
    exit 1
fi

# We deliberately exclude the messages.embedding column from the dump. The
# vectors are ~144 MB of incompressible floats (the bulk of the dump) and are
# fully regenerable from message content — restore.sh repopulates them via
# `backfill-embeddings` and rebuilds the ivfflat index. This keeps dumps ~10x
# smaller (~25 MB vs ~210 MB). pg_dump cannot exclude a single column, so we:
#   1. dump schema + all data EXCEPT the messages rows (and the regenerable
#      bakeoff tables, which are normally absent — the excludes are no-ops then);
#   2. append the messages rows via an explicit column list that omits both
#      `embedding` and the generated `content_tsv` column (recomputed on insert).
# The appended COPY lands after pg_dump's CREATE INDEX statements, so a plain
# `psql < dump` restores cleanly with embeddings left NULL, ready for backfill.
echo "[backup] dumping $DB_NAME (via $CONTAINER, excluding embedding vectors) → $TMPFILE"
MSG_COLS="id, conversation_id, message_uuid, role, content, created_at, sequence_num"
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
} | "$GZIP" -9 > "$TMPFILE"
SIZE="$(du -sh "$TMPFILE" | cut -f1)"
echo "[backup] dump complete: $SIZE compressed"

# Sanity check: a valid gzipped SQL dump is never this small.
BYTES="$(stat -f%z "$TMPFILE")"
if [ "$BYTES" -lt 1000 ]; then
    echo "[backup] ERROR: dump is only ${BYTES} bytes — likely failed, not uploading" >&2
    exit 1
fi

S3_KEY="${PREFIX}/${FILENAME}"
echo "[backup] uploading → s3://${BUCKET}/${S3_KEY}"
"$AWS" s3 cp "$TMPFILE" "s3://${BUCKET}/${S3_KEY}" \
    --profile "$BACKUP_AWS_PROFILE" \
    --region "$REGION" \
    --no-progress

echo "[backup] done: s3://${BUCKET}/${S3_KEY} ($SIZE)"
