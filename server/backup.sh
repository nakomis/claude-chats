#!/bin/sh
set -e
FILENAME="claude_chats_$(date +%Y%m%d_%H%M%S).sql.gz"
echo "$(date): Starting backup → s3://${BUCKET_NAME}/${FILENAME}"
pg_dump -h postgres -U claude claude_chats | gzip | aws s3 cp - "s3://${BUCKET_NAME}/${FILENAME}"
echo "$(date): Backup complete: ${FILENAME}"
