#!/usr/bin/env bash
# install.sh — set up claude-chats on a single machine.
#
# Everything runs here: PostgreSQL in Docker, the embedding provider, the hook
# and the MCP server. Nothing talks to another host, there is no message broker
# and no server component. For the distributed edition see ../install-client.sh
# and ../server/install.sh.
#
# ── Capture modes ─────────────────────────────────────────────────────────────
#
#   direct    The hook embeds each message and writes it to Postgres inline.
#             Fewest moving parts, and a message is searchable the moment the
#             session stops. Capture depends on Docker, Postgres and the
#             embedding provider all being up; when they are not, the message
#             lands in the fallback file and waits for --replay-fallback.
#
#   durable   The hook appends to a local SQLite outbox and does no network I/O
#             at all. A LaunchAgent runs `drain-outbox` every two minutes to
#             embed and write to Postgres. A stopped database becomes a delay
#             rather than a lost message, at the cost of one more moving part
#             and a couple of minutes' lag before a message is searchable.
#
#   Re-run this script with a different --mode to switch. Switching from durable
#   to direct drains the outbox first, so nothing is stranded.
#
# ── Embedding provider ────────────────────────────────────────────────────────
#
#   CLAUDE_CHATS_PROVIDER=ollama   (default) — requires Ollama running locally
#     OLLAMA_BASE_URL     default: http://localhost:11434
#     CLAUDE_CHATS_MODEL  default: mxbai-embed-large
#
#   CLAUDE_CHATS_PROVIDER=bedrock  — uses your AWS credentials / profile
#     CLAUDE_CHATS_MODEL  default: amazon.titan-embed-text-v2:0
#
#   CLAUDE_CHATS_PROVIDER=openai   — OPENAI_API_KEY must be set
#     CLAUDE_CHATS_MODEL  default: text-embedding-3-small
#
#   CLAUDE_CHATS_DIMENSIONS  output dimensions (default: 1024, matching init.sql)
#
# Re-running is safe — every step is idempotent.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${HOME}/.claude-chats"
CONFIG_FILE="${STATE_DIR}/config.env"
SETTINGS_FILE="${HOME}/.claude/settings.json"
LABEL="com.nakomis.claude-chats-drain"
PLIST_SRC="${REPO_DIR}/scripts/${LABEL}.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DB_URL="postgresql://claude:claude@localhost:5433/claude_chats"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }
step() { echo -e "\n${YELLOW}▶${NC} $*"; }

usage() {
    # The header comment block, minus the shebang and the leading '# '.
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
        "${BASH_SOURCE[0]}"
    cat <<'USAGE'

Usage:
  ./install.sh                     install, prompting for capture mode
  ./install.sh --mode direct       install or switch to direct capture
  ./install.sh --mode durable      install or switch to durable capture

Maintenance:
  ./install.sh --drain             drain the outbox into Postgres now
  ./install.sh --backfill          re-embed messages that have no vector
  ./install.sh --replay-fallback   recover messages from the fallback file
  ./install.sh --backup [path]     dump the database to a local file
  ./install.sh --restore [file]    restore from a dump, then backfill
  ./install.sh --uninstall         remove the hook, MCP and LaunchAgent
USAGE
}

# ---------------------------------------------------------------------------
# Config file — the record of what was installed, read back by the maintenance
# commands so they use the same provider the hook does.
# ---------------------------------------------------------------------------

# Read one key back out of the config file. Deliberately not `source`: the
# config is a remembered default, and sourcing it would clobber anything the
# user passed on the command line for this run.
config_get() {
    [[ -f "$CONFIG_FILE" ]] || return 0
    sed -n "s/^$1=//p" "$CONFIG_FILE" | tail -1
}

write_config() {
    mkdir -p "$STATE_DIR"
    {
        echo "# Written by claude-chats localhost install.sh — edit via a re-run."
        echo "CLAUDE_CHATS_CAPTURE_MODE=${MODE}"
        echo "CLAUDE_CHATS_PROVIDER=${PROVIDER}"
        echo "CLAUDE_CHATS_MODEL=${MODEL}"
        echo "CLAUDE_CHATS_DIMENSIONS=${DIMENSIONS}"
        echo "CLAUDE_CHATS_DB_URL=${DB_URL}"
        case "$PROVIDER" in
            ollama) echo "OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://localhost:11434}" ;;
            openai) echo "OPENAI_API_KEY=${OPENAI_API_KEY}" ;;
            bedrock)
                [[ -n "${AWS_PROFILE:-}" ]] && echo "AWS_PROFILE=${AWS_PROFILE}"
                [[ -n "${AWS_REGION:-}"  ]] && echo "AWS_REGION=${AWS_REGION}"
                ;;
        esac
        [[ -n "${SSL_CERT_FILE:-}" ]] && echo "SSL_CERT_FILE=${SSL_CERT_FILE}"
        # The guarded echo above is the last command in this block; without a
        # trailing success `set -e` would abort here whenever it is skipped.
        true
    } > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"   # it may hold an API key
}

# Run a hook console script with the recorded configuration, letting anything
# already in the environment win over the remembered value.
run_hook_cmd() {
    local envs=() line key
    if [[ -f "$CONFIG_FILE" ]]; then
        while IFS= read -r line; do
            [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
            key="${line%%=*}"
            [[ -n "${!key:-}" ]] && continue
            envs+=("$line")
        done < "$CONFIG_FILE"
    fi
    # ${envs[@]+...} because bash 3.2 (still what macOS ships) treats an empty
    # array as unbound under `set -u`.
    env ${envs[@]+"${envs[@]}"} uv run --project "${REPO_DIR}/hook" "$@"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

ACTION="install"
MODE_ARG=""
EXTRA_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)            MODE_ARG="${2:-}"; shift 2 ;;
        --mode=*)          MODE_ARG="${1#*=}"; shift ;;
        --drain)           ACTION="drain"; shift ;;
        --backfill)        ACTION="backfill"; shift ;;
        --replay-fallback) ACTION="replay"; shift ;;
        --backup)          ACTION="backup"; EXTRA_ARG="${2:-}"; [[ -n "$EXTRA_ARG" ]] && shift; shift ;;
        --restore)         ACTION="restore"; EXTRA_ARG="${2:-}"; [[ -n "$EXTRA_ARG" ]] && shift; shift ;;
        --uninstall)       ACTION="uninstall"; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 die "Unknown argument '$1'. Try --help." ;;
    esac
done

if [[ -n "$MODE_ARG" && "$MODE_ARG" != "direct" && "$MODE_ARG" != "durable" ]]; then
    die "--mode must be 'direct' or 'durable' (got '${MODE_ARG}')."
fi

# ---------------------------------------------------------------------------
# Maintenance actions — these do not reinstall anything
# ---------------------------------------------------------------------------

case "$ACTION" in
    drain)
        step "Draining the outbox"
        run_hook_cmd drain-outbox
        exit $?
        ;;
    backfill)
        step "Backfilling embeddings"
        run_hook_cmd backfill-embeddings
        exit $?
        ;;
    replay)
        # The fallback file replays into the outbox in both modes — it is simply
        # the staging table this recovery path knows how to write — so the drain
        # afterwards is what actually lands the messages in Postgres.
        step "Replaying the fallback file into the outbox"
        run_hook_cmd replay-fallback
        step "Draining the outbox"
        run_hook_cmd drain-outbox
        exit $?
        ;;
    backup)
        exec "${REPO_DIR}/scripts/backup.sh" ${EXTRA_ARG:+"$EXTRA_ARG"}
        ;;
    restore)
        exec "${REPO_DIR}/scripts/restore.sh" ${EXTRA_ARG:+"$EXTRA_ARG"}
        ;;
    uninstall)
        step "Removing the drain LaunchAgent"
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
        rm -f "$PLIST_DEST"
        ok "LaunchAgent removed"

        step "Unregistering the MCP server"
        claude mcp remove conversation-memory --scope user  2>/dev/null || true
        claude mcp remove conversation-memory --scope local 2>/dev/null || true
        ok "MCP unregistered"

        step "Removing hook entries from ${SETTINGS_FILE}"
        if [[ -f "$SETTINGS_FILE" ]]; then
            python3 - "$SETTINGS_FILE" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
hooks = data.get("hooks", {})
for event in ("Stop", "UserPromptSubmit"):
    groups = hooks.get(event, [])
    kept = [
        g for g in groups
        if not any(
            isinstance(h, dict) and "record-conversation" in h.get("command", "")
            for h in g.get("hooks", [])
        )
    ]
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
        fi
        ok "Hook entries removed"

        echo ""
        warn "The database container, its volume and ${STATE_DIR} were left alone."
        echo "  Remove them yourself if you mean to:"
        echo "    docker compose -f ${REPO_DIR}/docker-compose.yml down -v"
        echo "    rm -rf ${STATE_DIR}"
        exit 0
        ;;
esac

# ---------------------------------------------------------------------------
# Resolve capture mode: flag > previously installed > prompt
# ---------------------------------------------------------------------------

PREVIOUS_MODE="$(config_get CLAUDE_CHATS_CAPTURE_MODE)"

if [[ -n "$MODE_ARG" ]]; then
    MODE="$MODE_ARG"
elif [[ -n "$PREVIOUS_MODE" ]]; then
    MODE="$PREVIOUS_MODE"
    warn "Keeping the installed capture mode: ${MODE} (change it with --mode)"
else
    cat <<'PROMPT'

Capture mode:

  1) direct   The hook embeds and writes straight to Postgres.
              Simplest, and searchable immediately. If Docker, Postgres or
              the embedding provider is down, the message goes to a fallback
              file and waits to be replayed.

  2) durable  The hook appends to a local SQLite outbox and does no network
              I/O; a LaunchAgent drains it into Postgres every two minutes.
              Survives Postgres being down. One more moving part, and a
              couple of minutes before a message becomes searchable.

PROMPT
    read -rp "Choose [1/2] (default 2): " CHOICE
    case "${CHOICE:-2}" in
        1) MODE="direct" ;;
        2) MODE="durable" ;;
        *) die "Expected 1 or 2." ;;
    esac
fi

# Provider settings: this run's environment first, then what was installed
# last time, then the default. So a plain re-run keeps your choices, and
# `CLAUDE_CHATS_PROVIDER=openai ./install.sh` still switches provider.
PROVIDER="${CLAUDE_CHATS_PROVIDER:-$(config_get CLAUDE_CHATS_PROVIDER)}"
PROVIDER="${PROVIDER:-ollama}"
DIMENSIONS="${CLAUDE_CHATS_DIMENSIONS:-$(config_get CLAUDE_CHATS_DIMENSIONS)}"
DIMENSIONS="${DIMENSIONS:-1024}"

_DEFAULT_MODELS_ollama="mxbai-embed-large"
_DEFAULT_MODELS_bedrock="amazon.titan-embed-text-v2:0"
_DEFAULT_MODELS_openai="text-embedding-3-small"
_default_model_var="_DEFAULT_MODELS_${PROVIDER}"
MODEL="${CLAUDE_CHATS_MODEL:-}"
# Only inherit the remembered model if the provider has not changed — a Titan
# model name against Ollama would fail in a thoroughly confusing way.
if [[ -z "$MODEL" && "$(config_get CLAUDE_CHATS_PROVIDER)" == "$PROVIDER" ]]; then
    MODEL="$(config_get CLAUDE_CHATS_MODEL)"
fi
MODEL="${MODEL:-${!_default_model_var:-mxbai-embed-large}}"

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
step "Checking prerequisites (mode: ${MODE}, provider: ${PROVIDER})"

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

if docker compose version &>/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    die "Neither 'docker compose' nor 'docker-compose' found."
fi
ok "docker compose"

case "$PROVIDER" in
    ollama)
        require ollama
        ;;
    bedrock)
        if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_ACCESS_KEY_ID:-}" && ! -f "${HOME}/.aws/credentials" ]]; then
            warn "No AWS credentials found. Configure AWS_PROFILE or ~/.aws/credentials before using the hook."
        else
            ok "AWS credentials look configured"
        fi
        ;;
    openai)
        [[ -n "${OPENAI_API_KEY:-}" ]] || die "OPENAI_API_KEY must be set for the openai provider."
        ok "OPENAI_API_KEY is set"
        ;;
    *)
        die "Unknown provider '${PROVIDER}'. Expected 'ollama', 'bedrock', or 'openai'."
        ;;
esac

if [[ "$MODE" == "durable" && "$(uname -s)" != "Darwin" ]]; then
    warn "Durable mode's drainer is scheduled with launchd, which is macOS-only."
    warn "Everything else works; see INSTALL.md for the systemd-user equivalent."
fi

# ---------------------------------------------------------------------------
# 2. Start PostgreSQL
# ---------------------------------------------------------------------------
step "Starting PostgreSQL"

$COMPOSE -f "${REPO_DIR}/docker-compose.yml" up -d
echo -n "   Waiting for PostgreSQL to be ready"
for i in $(seq 1 30); do
    if docker exec claude-chats-db pg_isready -U claude -d claude_chats &>/dev/null; then
        echo ""; ok "PostgreSQL ready"; break
    fi
    echo -n "."
    sleep 1
    [[ $i -eq 30 ]] && { echo ""; die "PostgreSQL did not become ready in time."; }
done

# init.sql only runs on a first-time volume create, so apply it every time —
# it is written to be idempotent, and this is what picks up schema changes on
# an existing database.
docker exec -i claude-chats-db psql -q -U claude -d claude_chats < "${REPO_DIR}/init.sql" >/dev/null
ok "Schema applied"

# ---------------------------------------------------------------------------
# 3. Pull the Ollama model
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
# 4. Python dependencies
# ---------------------------------------------------------------------------
step "Installing hook dependencies"
uv sync --project "${REPO_DIR}/hook"
ok "hook deps installed"

step "Installing MCP dependencies"
uv sync --project "${REPO_DIR}/mcp"
ok "MCP deps installed"

# ---------------------------------------------------------------------------
# 5. Drain before switching durable -> direct
# ---------------------------------------------------------------------------
# Anything still pending in the outbox has no other route into Postgres once
# direct mode is in force, so it has to go now or not at all.
if [[ "$PREVIOUS_MODE" == "durable" && "$MODE" == "direct" ]]; then
    step "Draining the outbox before switching to direct mode"
    if run_hook_cmd drain-outbox; then
        ok "Outbox drained"
    else
        die "Outbox did not drain — refusing to switch to direct mode and strand it.
   Fix the problem (is Postgres up?), then re-run with --mode direct."
    fi
fi

# ---------------------------------------------------------------------------
# 6. Record the configuration
# ---------------------------------------------------------------------------
write_config
ok "Configuration written to ${CONFIG_FILE}"

# ---------------------------------------------------------------------------
# 7. Register the MCP server
# ---------------------------------------------------------------------------
step "Registering MCP server with Claude Code"

# Always re-register so env vars stay current — remove from all scopes first to
# avoid a stale entry in the other scope shadowing this one.
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
    ollama) MCP_ENV_ARGS+=(--env "OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://localhost:11434}") ;;
    openai) MCP_ENV_ARGS+=(--env "OPENAI_API_KEY=${OPENAI_API_KEY}") ;;
    bedrock)
        [[ -n "${AWS_PROFILE:-}"        ]] && MCP_ENV_ARGS+=(--env "AWS_PROFILE=${AWS_PROFILE}")
        [[ -n "${AWS_REGION:-}"         ]] && MCP_ENV_ARGS+=(--env "AWS_REGION=${AWS_REGION}")
        [[ -n "${AWS_DEFAULT_REGION:-}" ]] && MCP_ENV_ARGS+=(--env "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}")
        ;;
esac

claude mcp add conversation-memory --scope user "${MCP_ENV_ARGS[@]}" \
    -- uv run --project "${REPO_DIR}/mcp" conversation-memory-mcp
ok "MCP 'conversation-memory' registered"

# ---------------------------------------------------------------------------
# 8. Configure the hooks
# ---------------------------------------------------------------------------
step "Configuring hooks in ${SETTINGS_FILE}"

# Direct mode has to embed and reach Postgres from inside the hook, so it needs
# the provider settings on the command line. Durable mode touches nothing but a
# local file, and deliberately carries no configuration at all.
HOOK_ENV="CLAUDE_CHATS_CAPTURE_MODE=${MODE}"
if [[ "$MODE" == "direct" ]]; then
    HOOK_ENV+=" CLAUDE_CHATS_DB_URL=${DB_URL}"
    HOOK_ENV+=" CLAUDE_CHATS_PROVIDER=${PROVIDER}"
    HOOK_ENV+=" CLAUDE_CHATS_MODEL=${MODEL}"
    HOOK_ENV+=" CLAUDE_CHATS_DIMENSIONS=${DIMENSIONS}"
    case "$PROVIDER" in
        ollama) HOOK_ENV+=" OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://localhost:11434}" ;;
        openai) HOOK_ENV+=" OPENAI_API_KEY=${OPENAI_API_KEY}" ;;
        bedrock)
            [[ -n "${AWS_PROFILE:-}" ]] && HOOK_ENV+=" AWS_PROFILE=${AWS_PROFILE}"
            [[ -n "${AWS_REGION:-}"  ]] && HOOK_ENV+=" AWS_REGION=${AWS_REGION}"
            ;;
    esac
fi
[[ -n "${SSL_CERT_FILE:-}" ]] && HOOK_ENV+=" SSL_CERT_FILE=${SSL_CERT_FILE}"

HOOK_CMD="${HOOK_ENV} uv run --project \"${REPO_DIR}/hook\" record-conversation"

mkdir -p "$(dirname "$SETTINGS_FILE")"
[[ -f "$SETTINGS_FILE" ]] || echo '{}' > "$SETTINGS_FILE"

python3 - "$SETTINGS_FILE" "$HOOK_CMD" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]
with open(settings_path) as f:
    data = json.load(f)

hook_group = {
    "matcher": "",
    "hooks": [{"type": "command", "command": hook_cmd, "timeout": 120}],
}

hooks = data.setdefault("hooks", {})

# Drop any existing record-conversation entry before adding ours back. This is
# what keeps re-runs idempotent, and what makes a mode switch take effect rather
# than leaving two hooks racing each other.
for event in ("Stop", "UserPromptSubmit"):
    cleaned = [
        g for g in hooks.get(event, [])
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
ok "Stop + UserPromptSubmit hooks updated (mode: ${MODE})"

# ---------------------------------------------------------------------------
# 9. The drain LaunchAgent — installed in durable mode, removed in direct
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
    if [[ "$MODE" == "durable" ]]; then
        step "Installing the drain LaunchAgent"
        UV_BIN="$(command -v uv)"
        mkdir -p "$(dirname "$PLIST_DEST")"

        # Rendered in Python rather than sed: the environment block is a
        # multi-line XML fragment, and sed with embedded newlines is a trap.
        python3 - "$PLIST_SRC" "$PLIST_DEST" "$REPO_DIR" "$UV_BIN" "$CONFIG_FILE" <<'PYEOF'
import sys, xml.sax.saxutils as x

src, dest, repo, uv_bin, config_file = sys.argv[1:6]

# launchd inherits almost nothing, so everything the drainer needs to reach
# Postgres and the embedding provider is baked into the plist.
env = {}
with open(config_file) as fh:
    for line in fh:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == "CLAUDE_CHATS_CAPTURE_MODE":
            continue  # meaningless to the drainer
        env[k] = v

entries = "\n".join(
    f"        <key>{x.escape(k)}</key>\n        <string>{x.escape(v)}</string>"
    for k, v in sorted(env.items())
)

text = open(src).read()
text = text.replace("REPO_PATH", repo).replace("UV_PATH", uv_bin)
text = text.replace("ENV_ENTRIES", entries)
if "REPO_PATH" in text or "UV_PATH" in text or "ENV_ENTRIES" in text:
    sys.exit("plist template still has placeholders — has it changed shape?")
open(dest, "w").write(text)
PYEOF

        plutil -lint "$PLIST_DEST" >/dev/null
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
        ok "Drainer runs every 2 minutes (log: /tmp/claude-chats-drain.log)"
    else
        step "Removing the drain LaunchAgent (not used in direct mode)"
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
        rm -f "$PLIST_DEST"
        ok "LaunchAgent removed"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}All done!${NC}"
echo ""
echo "  Capture mode : ${MODE}"
echo "  Provider     : ${PROVIDER}"
echo "  Model        : ${MODEL}"
echo "  Dimensions   : ${DIMENSIONS}"
echo "  Database     : ${DB_URL}"
echo "  MCP tools    : search_memory · get_conversation · list_recent_sessions"
echo ""
if [[ "$MODE" == "durable" ]]; then
    echo "  Messages land in ${STATE_DIR}/outbox.db and reach Postgres within"
    echo "  ~2 minutes. Force it with:  ./install.sh --drain"
else
    echo "  Messages are written to Postgres as each session stops."
fi
echo ""
echo "  Switch mode:      ./install.sh --mode direct|durable"
echo "  Stop the DB:      docker compose -f ${REPO_DIR}/docker-compose.yml down"
echo "  Restart Claude Code for the MCP server to appear."
