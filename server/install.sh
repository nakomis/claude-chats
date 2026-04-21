#!/usr/bin/env bash
# server/install.sh — set up claude-chats server components on macOS
#
# What this script does:
#   1. Checks prerequisites
#   2. Reads / writes server/.env for persistent config
#   3. Starts PostgreSQL + ActiveMQ + backup containers (docker compose --profile server)
#   4. Deploys the CDK stack (S3 bucket + retention Lambda)
#   5. Downloads the Ollama embedding model
#   6. Installs a launchd agent for Ollama (restart on reboot)
#   7. Builds the TypeScript worker and installs a launchd agent for it
#
# Re-running is safe — all steps are idempotent.
#
# Required env vars (or set interactively):
#   AWS_PROFILE   — AWS profile to use for CDK + backups
#   AWS_REGION    — AWS region for the S3 bucket
#
# Run from the repo root:
#   bash server/install.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="${REPO_DIR}/server"
ENV_FILE="${SERVER_DIR}/.env"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }
step() { echo -e "\n${YELLOW}▶${NC} $*"; }

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
step "Checking prerequisites"

require() {
    command -v "$1" &>/dev/null || die "'$1' not found — please install it first."
    ok "$1"
}

require docker
require node
require npm
require aws
require ollama

NODE_MAJOR=$(node --version | sed 's/v\([0-9]*\).*/\1/')
if [[ "$NODE_MAJOR" -lt 18 ]]; then
    die "Node.js 18+ required (found $(node --version))"
fi
ok "node $(node --version)"

if docker compose version &>/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    die "Neither 'docker compose' nor 'docker-compose' found."
fi
ok "docker compose"

if ! command -v cdk &>/dev/null; then
    warn "'cdk' not found — installing globally via npm"
    npm install -g aws-cdk
fi
ok "cdk"

# ---------------------------------------------------------------------------
# 2. Config — read .env or prompt
# ---------------------------------------------------------------------------
step "Loading configuration"

load_env() {
    [[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
}
load_env

prompt_if_missing() {
    local var="$1" prompt="$2" default="${3:-}"
    if [[ -z "${!var:-}" ]]; then
        if [[ -n "$default" ]]; then
            read -rp "  ${prompt} [${default}]: " val
            eval "${var}=\"${val:-$default}\""
        else
            read -rp "  ${prompt}: " val
            [[ -z "$val" ]] && die "${var} is required."
            eval "${var}=\"$val\""
        fi
    fi
    ok "${var}=${!var}"
}

prompt_if_missing AWS_PROFILE  "AWS profile"          "${AWS_PROFILE:-default}"
prompt_if_missing AWS_REGION   "AWS region"           "${AWS_REGION:-eu-west-1}"
CLAUDE_CHATS_MODEL="${CLAUDE_CHATS_MODEL:-mxbai-embed-large}"

# Persist config
cat > "$ENV_FILE" <<EOF
AWS_PROFILE=${AWS_PROFILE}
AWS_REGION=${AWS_REGION}
CLAUDE_CHATS_MODEL=${CLAUDE_CHATS_MODEL}
EOF
# BUCKET_NAME added after CDK deploy below

# ---------------------------------------------------------------------------
# 3. Start containers
# ---------------------------------------------------------------------------
step "Starting Docker containers (postgres + activemq + backup)"

cd "$SERVER_DIR"

# Build the backup image first
$COMPOSE --profile server build backup

if docker ps --format '{{.Names}}' | grep -q '^claude-chats-db$'; then
    ok "Postgres container already running"
else
    $COMPOSE --profile server up -d
    echo -n "   Waiting for PostgreSQL"
    for i in $(seq 1 30); do
        if docker exec claude-chats-db pg_isready -U claude -d claude_chats &>/dev/null; then
            echo ""; ok "PostgreSQL ready"; break
        fi
        echo -n "."; sleep 1
        [[ $i -eq 30 ]] && echo "" && die "PostgreSQL did not become ready in time."
    done
fi

echo -n "   Waiting for ActiveMQ"
for i in $(seq 1 30); do
    if curl -sf http://localhost:8161/ &>/dev/null; then
        echo ""; ok "ActiveMQ ready"; break
    fi
    echo -n "."; sleep 2
    [[ $i -eq 30 ]] && echo "" && warn "ActiveMQ not yet responding — continuing anyway."
done

# ---------------------------------------------------------------------------
# 4. CDK deploy — S3 bucket + retention Lambda
# ---------------------------------------------------------------------------
step "Deploying CDK stack (S3 + retention Lambda)"

cd "${SERVER_DIR}/cdk"
npm ci --silent
export AWS_PROFILE AWS_REGION

# Bootstrap once — safe to re-run
AWS_PROFILE="$AWS_PROFILE" cdk bootstrap "aws://unknown/${AWS_REGION}" 2>/dev/null || true

AWS_PROFILE="$AWS_PROFILE" cdk deploy --require-approval never --outputs-file /tmp/cdk-outputs.json

BUCKET_NAME=$(python3 -c "import json; d=json.load(open('/tmp/cdk-outputs.json')); print(list(d.values())[0]['BucketName'])")
ok "S3 bucket: ${BUCKET_NAME}"

# Persist bucket name
echo "BUCKET_NAME=${BUCKET_NAME}" >> "$ENV_FILE"

# Restart backup container now that BUCKET_NAME is known
cd "$SERVER_DIR"
$COMPOSE --profile server up -d backup
ok "Backup container updated with bucket name"

# ---------------------------------------------------------------------------
# 5. Ollama model
# ---------------------------------------------------------------------------
step "Pulling Ollama model (${CLAUDE_CHATS_MODEL})"

if ollama list 2>/dev/null | grep -q "^${CLAUDE_CHATS_MODEL}"; then
    ok "Model '${CLAUDE_CHATS_MODEL}' already present"
else
    ollama pull "$CLAUDE_CHATS_MODEL"
    ok "Model '${CLAUDE_CHATS_MODEL}' pulled"
fi

# ---------------------------------------------------------------------------
# 6. Launchd — Ollama
# ---------------------------------------------------------------------------
step "Installing Ollama launchd agent"

OLLAMA_BIN="$(command -v ollama)"
OLLAMA_PLIST="${HOME}/Library/LaunchAgents/com.nakomis.ollama.plist"

cat > "$OLLAMA_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nakomis.ollama</string>
    <key>ProgramArguments</key>
    <array>
        <string>${OLLAMA_BIN}</string>
        <string>serve</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ollama.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ollama.err</string>
</dict>
</plist>
PLIST

launchctl unload "$OLLAMA_PLIST" 2>/dev/null || true
launchctl load "$OLLAMA_PLIST"
ok "Ollama launchd agent installed"

# ---------------------------------------------------------------------------
# 7. TypeScript worker — build + launchd
# ---------------------------------------------------------------------------
step "Building TypeScript worker"

cd "${SERVER_DIR}/worker"
npm ci --silent
npm run build
ok "Worker built"

NODE_BIN="$(command -v node)"
WORKER_PLIST="${HOME}/Library/LaunchAgents/com.nakomis.claude-chats-worker.plist"

cat > "$WORKER_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nakomis.claude-chats-worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>${NODE_BIN}</string>
        <string>${SERVER_DIR}/worker/dist/worker.js</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SERVER_DIR}/worker</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDE_CHATS_DB_URL</key>
        <string>postgresql://claude:claude@localhost:5433/claude_chats</string>
        <key>CLAUDE_CHATS_QUEUE_URL</key>
        <string>stomp://localhost:61613</string>
        <key>CLAUDE_CHATS_QUEUE_NAME</key>
        <string>claude-chats-messages</string>
        <key>CLAUDE_CHATS_MODEL</key>
        <string>${CLAUDE_CHATS_MODEL}</string>
        <key>OLLAMA_BASE_URL</key>
        <string>http://localhost:11434</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-chats-worker.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-chats-worker.err</string>
</dict>
</plist>
PLIST

launchctl unload "$WORKER_PLIST" 2>/dev/null || true
launchctl load "$WORKER_PLIST"
ok "Worker launchd agent installed"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}Server setup complete!${NC}"
echo ""
echo "  PostgreSQL  : localhost:5433"
echo "  ActiveMQ    : localhost:61613  (web console: http://localhost:8161)"
echo "  Ollama      : http://localhost:11434"
echo "  S3 bucket   : ${BUCKET_NAME}"
echo "  Worker logs : /tmp/claude-chats-worker.log"
echo ""
echo "  Now run on your client Mac:"
echo "    SERVER_HOST=<this-machine>.local bash install-client.sh"
