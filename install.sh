#!/usr/bin/env bash
# install.sh — set up claude-chats on a new machine
#
# What this script does:
#   1. Checks prerequisites (docker, docker compose, uv, ollama, claude, python 3.11+)
#   2. Starts the PostgreSQL container
#   3. Pulls the Ollama embedding model
#   4. Creates virtualenvs and installs dependencies (hook + mcp)
#   5. Registers the MCP server with Claude Code
#   6. Adds the Stop hook to ~/.claude/settings.json
#
# Re-running is safe — all steps are idempotent.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_CMD="uv run --project \"${REPO_DIR}/hook\" record-conversation"
MCP_CMD="uv run --project \"${REPO_DIR}/mcp\" conversation-memory-mcp"

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
require uv
require ollama
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
# 3. Pull Ollama embedding model
# ---------------------------------------------------------------------------
step "Pulling Ollama embedding model (mxbai-embed-large)"

MODEL="${CLAUDE_CHATS_MODEL:-mxbai-embed-large}"

if ollama list 2>/dev/null | grep -q "^${MODEL}"; then
    ok "Model '${MODEL}' already present"
else
    ollama pull "$MODEL"
    ok "Model '${MODEL}' pulled"
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

if claude mcp list 2>/dev/null | grep -q 'conversation-memory'; then
    ok "MCP 'conversation-memory' already registered — skipping"
else
    claude mcp add conversation-memory \
        --env CLAUDE_CHATS_DB_URL="postgresql://claude:claude@localhost:5433/claude_chats" \
        --env CLAUDE_CHATS_MODEL="${MODEL}" \
        --env OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}" \
        -- $MCP_CMD
    ok "MCP 'conversation-memory' registered"
fi

# ---------------------------------------------------------------------------
# 6. Add Stop hook to ~/.claude/settings.json
# ---------------------------------------------------------------------------
step "Configuring Stop hook in ~/.claude/settings.json"

SETTINGS_FILE="${HOME}/.claude/settings.json"
mkdir -p "$(dirname "$SETTINGS_FILE")"

# Build the hook entry we want to add
HOOK_ENTRY=$(cat <<EOF
{
  "type": "command",
  "command": "${HOOK_CMD}",
  "timeout": 120
}
EOF
)

HOOK_GROUP=$(cat <<EOF
{
  "matcher": "",
  "hooks": [${HOOK_ENTRY}]
}
EOF
)

if [[ ! -f "$SETTINGS_FILE" ]]; then
    # Create a fresh settings file
    echo '{"hooks":{"Stop":[]}}' | python3 -c "
import json, sys
data = json.load(sys.stdin)
data['hooks']['Stop'] = [json.loads('''${HOOK_GROUP}''')]
print(json.dumps(data, indent=2))
" > "$SETTINGS_FILE"
    ok "Created ${SETTINGS_FILE} with Stop hook"
else
    # Merge into existing file — only add if our command isn't already there
    if grep -qF "record-conversation" "$SETTINGS_FILE" 2>/dev/null; then
        ok "Stop hook already present in ${SETTINGS_FILE} — skipping"
    else
        python3 - "$SETTINGS_FILE" <<PYEOF
import json, sys

settings_path = sys.argv[1]
with open(settings_path) as f:
    data = json.load(f)

hook_entry = {
    "type": "command",
    "command": "${HOOK_CMD}",
    "timeout": 120
}
hook_group = {"matcher": "", "hooks": [hook_entry]}

hooks = data.setdefault("hooks", {})
stop  = hooks.setdefault("Stop", [])
stop.append(hook_group)

with open(settings_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
        ok "Stop hook appended to ${SETTINGS_FILE}"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}All done!${NC}"
echo ""
echo "  Database URL : postgresql://claude:claude@localhost:5433/claude_chats"
echo "  Embedding    : ${MODEL} via Ollama"
echo "  Hook         : fires on every Claude Code session stop"
echo "  MCP tools    : search_memory · get_conversation · list_recent_sessions"
echo ""
echo "  To stop the database:  docker compose -f ${REPO_DIR}/docker-compose.yml down"
echo "  To start it again:     docker compose -f ${REPO_DIR}/docker-compose.yml up -d"
