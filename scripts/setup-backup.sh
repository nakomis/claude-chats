#!/usr/bin/env bash
# One-shot setup for the daily claude-chats backup LaunchAgent.
# Run once after cloning / after rotating credentials.
# Prereqs: aws CLI configured with nakom.is-admin SSO profile and SSO session active.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="${REPO}/scripts/com.nakomis.claude-chats-backup.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/com.nakomis.claude-chats-backup.plist"
LAUNCHAGENTS_DIR="${HOME}/Library/LaunchAgents"
AWS_CREDS="${HOME}/.aws/credentials"
PROFILE="claude-chats-backup"
SSM_PROFILE="nakom.is-admin"
REGION="eu-west-2"

echo "=== claude-chats backup setup ==="
echo

# ── 1. AWS credentials ────────────────────────────────────────────────────────
echo "[1/3] Fetching taiga-backup credentials from SSM..."
KEY_ID="$(aws ssm get-parameter \
    --name /taiga/backup/aws-access-key-id \
    --with-decryption \
    --query Parameter.Value \
    --output text \
    --profile "$SSM_PROFILE" \
    --region "$REGION")"

KEY_SECRET="$(aws ssm get-parameter \
    --name /taiga/backup/aws-secret-access-key \
    --with-decryption \
    --query Parameter.Value \
    --output text \
    --profile "$SSM_PROFILE" \
    --region "$REGION")"

mkdir -p "$(dirname "$AWS_CREDS")"

# Remove existing profile block if present, then append fresh
if grep -q "\[${PROFILE}\]" "$AWS_CREDS" 2>/dev/null; then
    echo "  Replacing existing [${PROFILE}] block..."
    # Delete from [profile] line to the next blank line or end of file
    python3 - <<PYEOF "$AWS_CREDS" "$PROFILE"
import sys, re
path, profile = sys.argv[1], sys.argv[2]
with open(path) as f:
    text = f.read()
# Remove the profile block (header + its keys, up to the next header or EOF)
text = re.sub(r'\[' + re.escape(profile) + r'\][^\[]*', '', text)
with open(path, 'w') as f:
    f.write(text.rstrip('\n') + '\n')
PYEOF
fi

cat >> "$AWS_CREDS" <<CREDS

[${PROFILE}]
aws_access_key_id = ${KEY_ID}
aws_secret_access_key = ${KEY_SECRET}
region = ${REGION}
CREDS

echo "  Written [${PROFILE}] to ${AWS_CREDS}"

# ── 2. Install LaunchAgent ────────────────────────────────────────────────────
echo "[2/3] Installing LaunchAgent..."
mkdir -p "$LAUNCHAGENTS_DIR"
sed "s|REPO_PATH|${REPO}|g" "$PLIST_SRC" > "$PLIST_DEST"

# Unload if already loaded (ignore errors if not loaded)
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
echo "  Installed: ${PLIST_DEST}"

# ── 3. Smoke test ─────────────────────────────────────────────────────────────
echo "[3/3] Running a test backup..."
chmod +x "${REPO}/scripts/backup.sh" "${REPO}/scripts/restore.sh"
"${REPO}/scripts/backup.sh"

echo
echo "=== Setup complete ==="
echo "Backups run daily at 03:00. Log: ${REPO}/scripts/backup.log"
echo "To restore: ${REPO}/scripts/restore.sh"
