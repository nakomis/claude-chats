#!/usr/bin/env bash
# install-client.sh — configure the client Mac for client-server mode
#
# What this script does:
#   1. Checks prerequisites
#   2. Prompts for SERVER_HOST (the server machine's hostname/IP)
#   3. Installs Python dependencies for the hook and MCP server
#   4. Registers the MCP server pointing at the remote PostgreSQL
#   5. Configures the Stop/UserPromptSubmit hooks in remote mode
#      (hook posts messages to ActiveMQ; embedding happens on the server)
#
# Re-running is safe — all steps are idempotent.
#
# Usage:
#   SERVER_HOST=myserver.local bash install-client.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_CMD_ARGS=(uv run --project "${REPO_DIR}/mcp" conversation-memory-mcp)

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

require uv
require claude
require python3

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [[ "$PYTHON_MAJOR" -lt 3 || ("$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 11) ]]; then
    die "Python 3.11+ required (found $PYTHON_VERSION). Use 'asdf shell python 3.11.x' first."
fi
ok "python3 $PYTHON_VERSION"

# ---------------------------------------------------------------------------
# 2. Config
# ---------------------------------------------------------------------------
step "Configuration"

if [[ -z "${SERVER_HOST:-}" ]]; then
    read -rp "  Server hostname or IP (e.g. myserver.local): " SERVER_HOST
    [[ -z "$SERVER_HOST" ]] && die "SERVER_HOST is required."
fi
ok "SERVER_HOST=${SERVER_HOST}"

# Embedding provider for the MCP server (query embedding still happens on client)
PROVIDER="${CLAUDE_CHATS_PROVIDER:-ollama}"
DIMENSIONS="${CLAUDE_CHATS_DIMENSIONS:-1024}"

_DEFAULT_MODELS_ollama="mxbai-embed-large"
_DEFAULT_MODELS_bedrock="amazon.titan-embed-text-v2:0"
_DEFAULT_MODELS_openai="text-embedding-3-small"
_default_model_var="_DEFAULT_MODELS_${PROVIDER}"
MODEL="${CLAUDE_CHATS_MODEL:-${!_default_model_var:-mxbai-embed-large}}"

DB_URL="postgresql://claude:claude@${SERVER_HOST}:5433/claude_chats"
QUEUE_URL="stomp://${SERVER_HOST}:61613"

_SSL_PREFIX=""
[[ -n "${SSL_CERT_FILE:-}" ]] && _SSL_PREFIX="SSL_CERT_FILE=${SSL_CERT_FILE} "
HOOK_CMD="${_SSL_PREFIX}CLAUDE_CHATS_MODE=remote CLAUDE_CHATS_QUEUE_URL=${QUEUE_URL} uv run --project \"${REPO_DIR}/hook\" record-conversation"

# Provider-specific check for MCP query embeddings
case "$PROVIDER" in
    ollama)
        require ollama
        OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
        ;;
    bedrock)
        if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_ACCESS_KEY_ID:-}" && ! -f "${HOME}/.aws/credentials" ]]; then
            warn "No AWS credentials found. Needed for MCP query embeddings."
        else
            ok "AWS credentials configured"
        fi
        ;;
    openai)
        [[ -z "${OPENAI_API_KEY:-}" ]] && die "OPENAI_API_KEY must be set for the openai provider."
        ok "OPENAI_API_KEY is set"
        ;;
    *)
        die "Unknown provider '${PROVIDER}'. Expected 'ollama', 'bedrock', or 'openai'."
        ;;
esac

# ---------------------------------------------------------------------------
# 3. Install Python dependencies
# ---------------------------------------------------------------------------
step "Installing hook dependencies (includes stomp.py for remote mode)"
uv sync --project "${REPO_DIR}/hook"
ok "hook deps installed"

step "Installing MCP dependencies"
uv sync --project "${REPO_DIR}/mcp"
ok "MCP deps installed"

# ---------------------------------------------------------------------------
# 4. Register MCP server (pointing at remote DB)
# ---------------------------------------------------------------------------
step "Registering MCP server with Claude Code (remote DB: ${SERVER_HOST})"

claude mcp remove conversation-memory --scope user  2>/dev/null || true
claude mcp remove conversation-memory --scope local 2>/dev/null || true

MCP_ENV_ARGS=(
    --env "CLAUDE_CHATS_DB_URL=${DB_URL}"
    --env "CLAUDE_CHATS_PROVIDER=${PROVIDER}"
    --env "CLAUDE_CHATS_MODEL=${MODEL}"
    --env "CLAUDE_CHATS_DIMENSIONS=${DIMENSIONS}"
)

[[ -n "${SSL_CERT_FILE:-}" ]] && MCP_ENV_ARGS+=(--env "SSL_CERT_FILE=${SSL_CERT_FILE}")

case "$PROVIDER" in
    ollama)
        MCP_ENV_ARGS+=(--env "OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://localhost:11434}")
        ;;
    openai)
        MCP_ENV_ARGS+=(--env "OPENAI_API_KEY=${OPENAI_API_KEY}")
        ;;
    bedrock)
        [[ -n "${AWS_PROFILE:-}"        ]] && MCP_ENV_ARGS+=(--env "AWS_PROFILE=${AWS_PROFILE}")
        [[ -n "${AWS_REGION:-}"         ]] && MCP_ENV_ARGS+=(--env "AWS_REGION=${AWS_REGION}")
        [[ -n "${AWS_DEFAULT_REGION:-}" ]] && MCP_ENV_ARGS+=(--env "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}")
        ;;
esac

claude mcp add conversation-memory --scope user "${MCP_ENV_ARGS[@]}" -- "${MCP_CMD_ARGS[@]}"
ok "MCP 'conversation-memory' registered → ${SERVER_HOST}"

# ---------------------------------------------------------------------------
# 5. Configure hooks (remote mode)
# ---------------------------------------------------------------------------
step "Configuring hooks in ~/.claude/settings.json (remote mode)"

SETTINGS_FILE="${HOME}/.claude/settings.json"
mkdir -p "$(dirname "$SETTINGS_FILE")"
[[ ! -f "$SETTINGS_FILE" ]] && echo '{}' > "$SETTINGS_FILE"

python3 - "$SETTINGS_FILE" "$HOOK_CMD" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]
with open(settings_path) as f:
    data = json.load(f)

hook_entry = {"type": "command", "command": hook_cmd, "timeout": 30}
hook_group = {"matcher": "", "hooks": [hook_entry]}

hooks = data.setdefault("hooks", {})

for event in ("Stop", "UserPromptSubmit"):
    existing = hooks.get(event, [])
    cleaned = [
        g for g in existing
        if not any(
            isinstance(h, dict) and "record-conversation" in h.get("command", "")
            for h in g.get("hooks", [])
        )
    ]
    cleaned.append(hook_group)
    hooks[event] = cleaned

with open(settings_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
ok "Stop + UserPromptSubmit hooks updated (remote mode, timeout: 30s)"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}Client setup complete!${NC}"
echo ""
echo "  Mode         : remote"
echo "  Server       : ${SERVER_HOST}"
echo "  Database URL : ${DB_URL}"
echo "  Queue URL    : ${QUEUE_URL}"
echo "  MCP provider : ${PROVIDER} (${MODEL}) — used for search query embedding"
echo ""
echo "  The hook now posts messages to ActiveMQ on ${SERVER_HOST}."
echo "  Embedding and DB writes happen on the server."
