#!/usr/bin/env bash
# install.sh — set up claude-chats on a new machine
#
# What this script does:
#   1. Checks prerequisites (docker, docker compose, uv, claude, python 3.11+)
#   2. Starts the PostgreSQL container
#   3. Pulls the Ollama model (ollama provider only)
#   4. Creates virtualenvs and installs dependencies (hook + mcp)
#   5. Registers the MCP server with Claude Code
#   6. Adds the Stop hook to ~/.claude/settings.json
#
# Re-running is safe — all steps are idempotent.
#
# ── Embedding provider ────────────────────────────────────────────────────────
#
#   Set CLAUDE_CHATS_PROVIDER before running to choose a provider:
#
#   CLAUDE_CHATS_PROVIDER=ollama   (default)
#     Requires Ollama running locally.
#     OLLAMA_BASE_URL   default: http://localhost:11434
#     CLAUDE_CHATS_MODEL default: mxbai-embed-large
#
#   CLAUDE_CHATS_PROVIDER=bedrock
#     Uses Amazon Bedrock via your AWS credentials / profile.
#     AWS credentials must be configured (AWS_PROFILE, AWS_REGION, etc.)
#     CLAUDE_CHATS_MODEL default: amazon.titan-embed-text-v2:0
#       Also supported: cohere.embed-english-v3
#     No extra software to install — boto3 is included in the virtualenv.
#
#   CLAUDE_CHATS_PROVIDER=openai
#     OPENAI_API_KEY    must be set
#     CLAUDE_CHATS_MODEL default: text-embedding-3-small
#
#   CLAUDE_CHATS_DIMENSIONS  output dimensions (default: 1024)
#     Titan V2 supports 256 / 512 / 1024.
#     OpenAI 3-series supports arbitrary reduction.
#     Ollama and Cohere ignore this — their dimensions are fixed by the model.
#
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_SSL_PREFIX=""
[[ -n "${SSL_CERT_FILE:-}" ]] && _SSL_PREFIX="SSL_CERT_FILE=${SSL_CERT_FILE} "
HOOK_CMD="${_SSL_PREFIX}uv run --project \"${REPO_DIR}/hook\" record-conversation"
MCP_CMD_ARGS=(uv run --project "${REPO_DIR}/mcp" conversation-memory-mcp)

PROVIDER="${CLAUDE_CHATS_PROVIDER:-ollama}"
DIMENSIONS="${CLAUDE_CHATS_DIMENSIONS:-1024}"

_DEFAULT_MODELS_ollama="mxbai-embed-large"
_DEFAULT_MODELS_bedrock="amazon.titan-embed-text-v2:0"
_DEFAULT_MODELS_openai="text-embedding-3-small"

# Resolve default model for provider
_default_model_var="_DEFAULT_MODELS_${PROVIDER}"
MODEL="${CLAUDE_CHATS_MODEL:-${!_default_model_var:-mxbai-embed-large}}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }
step() { echo -e "\n${YELLOW}▶${NC} $*"; }

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
step "Checking prerequisites (provider: ${PROVIDER})"

require() {
    command -v "$1" &>/dev/null || die "'$1' not found — please install it first."
    ok "$1"
}

require docker
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

# docker compose (v2 plugin or standalone)
if docker compose version &>/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    die "Neither 'docker compose' nor 'docker-compose' found."
fi
ok "docker compose"

# Provider-specific prerequisite checks
case "$PROVIDER" in
    ollama)
        require ollama
        ;;
    bedrock)
        # boto3 is in the virtualenv; just verify AWS credentials exist
        if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_ACCESS_KEY_ID:-}" && ! -f "${HOME}/.aws/credentials" ]]; then
            warn "No AWS credentials found. Ensure AWS_PROFILE or ~/.aws/credentials is configured before using the hook/MCP."
        else
            ok "AWS credentials look configured"
        fi
        ;;
    openai)
        if [[ -z "${OPENAI_API_KEY:-}" ]]; then
            die "OPENAI_API_KEY must be set for the openai provider."
        fi
        ok "OPENAI_API_KEY is set"
        ;;
    *)
        die "Unknown provider '${PROVIDER}'. Expected 'ollama', 'bedrock', or 'openai'."
        ;;
esac

# ---------------------------------------------------------------------------
# 2. Start PostgreSQL container
# ---------------------------------------------------------------------------
step "Starting PostgreSQL container"

cd "$REPO_DIR"

if docker ps --format '{{.Names}}' | grep -q '^claude-chats-db$'; then
    ok "Container 'claude-chats-db' already running"
else
    $COMPOSE up -d
    echo -n "   Waiting for PostgreSQL to be ready"
    for i in $(seq 1 30); do
        if docker exec claude-chats-db pg_isready -U claude -d claude_chats &>/dev/null; then
            echo ""
            ok "PostgreSQL ready"
            break
        fi
        echo -n "."
        sleep 1
        if [[ $i -eq 30 ]]; then
            echo ""
            die "PostgreSQL did not become ready in time."
        fi
    done
fi

# ---------------------------------------------------------------------------
# 3. Pull Ollama model (ollama provider only)
# ---------------------------------------------------------------------------
if [[ "$PROVIDER" == "ollama" ]]; then
    step "Pulling Ollama model (${MODEL})"
    if ollama list 2>/dev/null | grep -q "^${MODEL}"; then
        ok "Model '${MODEL}' already present"
    else
        ollama pull "$MODEL"
        ok "Model '${MODEL}' pulled"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Install Python dependencies
# ---------------------------------------------------------------------------
step "Installing hook dependencies"
uv sync --project "${REPO_DIR}/hook"
ok "hook deps installed"

step "Installing MCP dependencies"
uv sync --project "${REPO_DIR}/mcp"
ok "MCP deps installed"

# ---------------------------------------------------------------------------
# 5. Register the MCP server with Claude Code
# ---------------------------------------------------------------------------
step "Registering MCP server with Claude Code"

# Always re-register so env vars stay current — remove from all scopes to avoid stale entries
claude mcp remove conversation-memory --scope user  2>/dev/null || true
claude mcp remove conversation-memory --scope local 2>/dev/null || true

MCP_ENV_ARGS=(
    --env "CLAUDE_CHATS_DB_URL=postgresql://claude:claude@localhost:5433/claude_chats"
    --env "CLAUDE_CHATS_PROVIDER=${PROVIDER}"
    --env "CLAUDE_CHATS_MODEL=${MODEL}"
    --env "CLAUDE_CHATS_DIMENSIONS=${DIMENSIONS}"
)

# Propagate SSL_CERT_FILE if set — needed on networks with TLS inspection
[[ -n "${SSL_CERT_FILE:-}" ]] && MCP_ENV_ARGS+=(--env "SSL_CERT_FILE=${SSL_CERT_FILE}")

case "$PROVIDER" in
    ollama)
        MCP_ENV_ARGS+=(--env "OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://localhost:11434}")
        ;;
    openai)
        MCP_ENV_ARGS+=(--env "OPENAI_API_KEY=${OPENAI_API_KEY}")
        ;;
    bedrock)
        [[ -n "${AWS_PROFILE:-}"     ]] && MCP_ENV_ARGS+=(--env "AWS_PROFILE=${AWS_PROFILE}")
        [[ -n "${AWS_REGION:-}"      ]] && MCP_ENV_ARGS+=(--env "AWS_REGION=${AWS_REGION}")
        [[ -n "${AWS_DEFAULT_REGION:-}" ]] && MCP_ENV_ARGS+=(--env "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}")
        ;;
esac

claude mcp add conversation-memory --scope user "${MCP_ENV_ARGS[@]}" -- "${MCP_CMD_ARGS[@]}"
ok "MCP 'conversation-memory' registered"

# ---------------------------------------------------------------------------
# 6. Add Stop hook to ~/.claude/settings.json
# ---------------------------------------------------------------------------
step "Configuring Stop hook in ~/.claude/settings.json"

SETTINGS_FILE="${HOME}/.claude/settings.json"
mkdir -p "$(dirname "$SETTINGS_FILE")"

if [[ ! -f "$SETTINGS_FILE" ]]; then
    echo '{}' > "$SETTINGS_FILE"
fi

python3 - "$SETTINGS_FILE" "$HOOK_CMD" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]
with open(settings_path) as f:
    data = json.load(f)

hook_entry = {
    "type": "command",
    "command": hook_cmd,
    "timeout": 120,
}
hook_group = {"matcher": "", "hooks": [hook_entry]}

hooks = data.setdefault("hooks", {})

# Remove any stale record-conversation entries so re-runs stay idempotent
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
ok "Stop + UserPromptSubmit hooks updated in ${SETTINGS_FILE}"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}All done!${NC}"
echo ""
echo "  Provider     : ${PROVIDER}"
echo "  Model        : ${MODEL}"
echo "  Dimensions   : ${DIMENSIONS}"
echo "  Database URL : postgresql://claude:claude@localhost:5433/claude_chats"
echo "  Hook         : fires on every Claude Code session stop"
echo "  MCP tools    : search_memory · get_conversation · list_recent_sessions"
echo ""
echo "  To switch provider, re-run with CLAUDE_CHATS_PROVIDER=bedrock|openai|ollama"
echo "  To stop the database:  docker compose -f ${REPO_DIR}/docker-compose.yml down"
echo "  To start it again:     docker compose -f ${REPO_DIR}/docker-compose.yml up -d"
