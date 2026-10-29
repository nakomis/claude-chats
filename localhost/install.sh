#!/usr/bin/env bash
# install.sh — set up claude-chats on a single machine.
#
# Everything runs here: PostgreSQL in Docker, the embedding provider, the hook
# and the MCP server. Nothing talks to another host, there is no message broker
# and no server component. For the distributed edition see ../README.md.
#
# The hook and the MCP server are the repository's own ../hook and ../mcp, the
# same code the distributed edition runs; this directory holds only what is
# specific to running everything on one machine.
#
# ── How capture works ─────────────────────────────────────────────────────────
#
#   The hook appends each new message to a local SQLite outbox and does no
#   network I/O itself, so capture cannot fail because Docker, Postgres or the
#   embedding provider is down. It then starts `drain-outbox` in the background,
#   which embeds the new messages and writes them to Postgres within seconds.
#   A LaunchAgent (macOS) or systemd user timer (Linux) also drains every two
#   minutes, as the backstop for anything that drain could not deliver.
#
#   Earlier versions offered a `direct` mode, in which the hook wrote Postgres
#   itself. It has been removed; re-running this script moves such an install
#   to the outbox, and nothing already recorded is touched.
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
# Re-running is safe — every step is idempotent. Re-running also upgrades an
# install made from an older checkout: the hook command and MCP registration are
# rewritten to point at ../hook and ../mcp, whatever they pointed at before.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

LOCALHOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${LOCALHOST_DIR}/.." && pwd)"
HOOK_DIR="${ROOT_DIR}/hook"
MCP_DIR="${ROOT_DIR}/mcp"
STATE_DIR="${HOME}/.claude-chats"
CONFIG_FILE="${STATE_DIR}/config.env"
SETTINGS_FILE="${HOME}/.claude/settings.json"
LABEL="com.nakomis.claude-chats-drain"
PLIST_SRC="${LOCALHOST_DIR}/launchd/${LABEL}.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
SYSTEMD_SRC="${LOCALHOST_DIR}/systemd"
SYSTEMD_DEST="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SYSTEMD_UNIT="claude-chats-drain"
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
  ./install.sh                     install, or upgrade an existing install

Maintenance:
  ./install.sh --drain             drain the outbox into Postgres now
  ./install.sh --backfill          re-embed messages that have no vector
  ./install.sh --replay-fallback   recover messages from the fallback file
  ./install.sh --backup [path]     dump the database to a local file
  ./install.sh --restore [file]    restore from a dump, then backfill
  ./install.sh --uninstall         remove the hook, MCP and drain schedule
USAGE
}

# ---------------------------------------------------------------------------
# Config file — the record of what was installed, read back by the maintenance
# commands and by every drain, so they use the same provider the hook does.
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
    local tmp="${CONFIG_FILE}.tmp.$$"
    # umask first, and a new file renamed into place: it may hold an API key,
    # and neither a fresh file nor a truncated old one should be readable by
    # anyone else, even for the moment before a chmod.
    (
    umask 077
    {
        echo "# Written by claude-chats localhost/install.sh — edit via a re-run."
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
                [[ -n "${AWS_DEFAULT_REGION:-}" ]] && echo "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}"
                ;;
        esac
        [[ -n "${SSL_CERT_FILE:-}" ]] && echo "SSL_CERT_FILE=${SSL_CERT_FILE}"
        # The guarded echo above is the last command in this block; without a
        # trailing success `set -e` would abort here whenever it is skipped.
        true
    } > "$tmp"
    )
    chmod 600 "$tmp"
    mv -f "$tmp" "$CONFIG_FILE"
}

# The recorded configuration as KEY=VALUE words for `env`, leaving out anything
# already in the environment so that it wins over the remembered value.
config_env() {
    CONFIG_ENV=()
    local line key
    [[ -f "$CONFIG_FILE" ]] || return 0
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
        key="${line%%=*}"
        [[ -n "${!key:-}" ]] && continue
        CONFIG_ENV+=("$line")
    done < "$CONFIG_FILE"
}

# Run a hook console script with the recorded configuration.
run_hook_cmd() {
    config_env
    # ${CONFIG_ENV[@]+...} because bash 3.2 (still what macOS ships) treats an
    # empty array as unbound under `set -u`.
    env ${CONFIG_ENV[@]+"${CONFIG_ENV[@]}"} uv run --project "$HOOK_DIR" "$@"
}

# Fail before changing anything if settings.json is there but is not JSON:
# rewriting it would mean either refusing half-way through an install or
# throwing away whatever the user had in it.
check_settings() {
    [[ -f "$SETTINGS_FILE" ]] || return 0
    python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' \
        "$SETTINGS_FILE" 2>/dev/null \
        || die "${SETTINGS_FILE} is not valid JSON. Fix it, then re-run; nothing was changed."
}

# Remove every hook that runs record-conversation, whichever checkout or
# edition it points at, from Stop / UserPromptSubmit / SessionEnd — just that
# hook, not the rest of a group it shares. With a command given, add that back
# as the one entry for each event. The original is kept as settings.json.bak,
# the new file is written beside it and renamed into place, and its
# indentation and any non-ASCII text are kept as they were.
rewrite_hooks() {
    python3 - "$SETTINGS_FILE" "${1:-}" <<'PYEOF'
import json, os, re, shutil, sys

settings_path, hook_cmd = sys.argv[1], sys.argv[2]
with open(settings_path, encoding="utf-8") as f:
    original = f.read()
data = json.loads(original) if original.strip() else {}

# The indentation the file already uses: the first indented line's.
m = re.search(r"\n([ \t]+)\S", original)
indent = m.group(1) if m else 2
if isinstance(indent, str) and set(indent) == {" "}:
    indent = len(indent)

def ours(h):
    return isinstance(h, dict) and "record-conversation" in h.get("command", "")

hooks = data.setdefault("hooks", {})
for event in ("Stop", "UserPromptSubmit", "SessionEnd"):
    kept = []
    for g in hooks.get(event, []):
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            rest = [h for h in g["hooks"] if not ours(h)]
            if not rest:
                continue  # the group was only ours
            g = {**g, "hooks": rest}
        kept.append(g)
    if hook_cmd:
        kept.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": hook_cmd, "timeout": 120}],
        })
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
if not hooks:
    data.pop("hooks", None)

text = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
if text == original:
    sys.exit(0)
shutil.copy2(settings_path, settings_path + ".bak")
tmp = f"{settings_path}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(text)
shutil.copymode(settings_path, tmp)
os.replace(tmp, settings_path)
PYEOF
}

# Fill in a launchd or systemd template.
render_template() {
    python3 - "$1" "$2" "$ROOT_DIR" "$UV_BIN" "$CONFIG_FILE" <<'PYEOF'
import sys

src, dest, repo, uv_bin, config_file = sys.argv[1:6]
text = open(src).read()
text = text.replace("REPO_PATH", repo).replace("UV_PATH", uv_bin)
text = text.replace("CONFIG_PATH", config_file)
open(dest, "w").write(text)
PYEOF
}

remove_schedule() {
    case "$(uname -s)" in
        Darwin)
            launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
            rm -f "$PLIST_DEST"
            ;;
        Linux)
            if command -v systemctl &>/dev/null; then
                systemctl --user disable --now "${SYSTEMD_UNIT}.timer" 2>/dev/null || true
            fi
            rm -f "${SYSTEMD_DEST}/${SYSTEMD_UNIT}.service" "${SYSTEMD_DEST}/${SYSTEMD_UNIT}.timer"
            if command -v systemctl &>/dev/null; then
                systemctl --user daemon-reload 2>/dev/null || true
            fi
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

ACTION="install"
EXTRA_ARG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode|--mode=*)
            # Kept so an old command line still means something: durable is now
            # the only behaviour, and direct is refused rather than ignored.
            if [[ "$1" == --mode=* ]]; then MODE_ARG="${1#*=}"; shift; else MODE_ARG="${2:-}"; shift 2; fi
            case "$MODE_ARG" in
                durable) warn "--mode is no longer needed: the outbox is the only capture mode." ;;
                direct)  die "Direct capture mode has been removed. Run ./install.sh without --mode;
   messages are written to Postgres within seconds by a background drain instead." ;;
                *)       die "Unknown --mode '${MODE_ARG}'." ;;
            esac
            ;;
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

# ---------------------------------------------------------------------------
# Maintenance actions — these do not reinstall anything
# ---------------------------------------------------------------------------

if [[ "$ACTION" == "install" || "$ACTION" == "uninstall" ]]; then
    check_settings
fi

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
        # The fallback file replays into the outbox, so the drain afterwards is
        # what actually lands the messages in Postgres.
        step "Replaying the fallback file into the outbox"
        run_hook_cmd replay-fallback
        step "Draining the outbox"
        run_hook_cmd drain-outbox
        exit $?
        ;;
    backup)
        exec "${ROOT_DIR}/scripts/backup.sh" --local ${EXTRA_ARG:+"$EXTRA_ARG"}
        ;;
    restore)
        # The backfill at the end of a restore embeds with the installed provider.
        config_env
        exec env ${CONFIG_ENV[@]+"${CONFIG_ENV[@]}"} \
            "${ROOT_DIR}/scripts/restore.sh" --local ${EXTRA_ARG:+"$EXTRA_ARG"}
        ;;
    uninstall)
        step "Removing the drain schedule"
        remove_schedule
        ok "Drain schedule removed"

        step "Unregistering the MCP server"
        claude mcp remove conversation-memory --scope user  2>/dev/null || true
        claude mcp remove conversation-memory --scope local 2>/dev/null || true
        ok "MCP unregistered"

        step "Removing hook entries from ${SETTINGS_FILE}"
        [[ -f "$SETTINGS_FILE" ]] && rewrite_hooks ""
        ok "Hook entries removed"

        echo ""
        warn "The database container, its volume and ${STATE_DIR} were left alone."
        echo "  Remove them yourself if you mean to:"
        echo "    docker compose -f ${LOCALHOST_DIR}/docker-compose.yml down -v"
        echo "    rm -rf ${STATE_DIR}"
        exit 0
        ;;
esac

# ---------------------------------------------------------------------------
# What is being upgraded from, if anything
# ---------------------------------------------------------------------------

PREVIOUS_MODE="$(config_get CLAUDE_CHATS_CAPTURE_MODE)"
if [[ "$PREVIOUS_MODE" == "direct" ]]; then
    warn "This install used direct capture mode, which has been removed."
    warn "Moving it to the outbox; everything already in Postgres stays where it is."
fi

# Provider settings: this run's environment first, then what was installed
# last time, then the default. So a plain re-run keeps your choices, and
# `CLAUDE_CHATS_PROVIDER=openai ./install.sh` still switches provider.
PROVIDER="${CLAUDE_CHATS_PROVIDER:-$(config_get CLAUDE_CHATS_PROVIDER)}"
PROVIDER="${PROVIDER:-ollama}"
DIMENSIONS="${CLAUDE_CHATS_DIMENSIONS:-$(config_get CLAUDE_CHATS_DIMENSIONS)}"
DIMENSIONS="${DIMENSIONS:-1024}"
# Same order for the provider's own settings, so a re-run from a shell that
# lacks them does not write them out of the config.
if [[ "$PROVIDER" == "ollama" ]]; then
    OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-$(config_get OLLAMA_BASE_URL)}"
fi
if [[ "$PROVIDER" == "openai" ]]; then
    OPENAI_API_KEY="${OPENAI_API_KEY:-$(config_get OPENAI_API_KEY)}"
fi
if [[ "$PROVIDER" == "bedrock" ]]; then
    AWS_PROFILE="${AWS_PROFILE:-$(config_get AWS_PROFILE)}"
    AWS_REGION="${AWS_REGION:-$(config_get AWS_REGION)}"
    AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(config_get AWS_DEFAULT_REGION)}"
fi
SSL_CERT_FILE="${SSL_CERT_FILE:-$(config_get SSL_CERT_FILE)}"

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

# ---------------------------------------------------------------------------
# 2. Start PostgreSQL
# ---------------------------------------------------------------------------
step "Starting PostgreSQL"

# The compose project is named after this directory ("localhost"), which is
# what names the data volume (localhost_pgdata): keep the file here, or an
# existing database would be orphaned rather than reused.
$COMPOSE -f "${LOCALHOST_DIR}/docker-compose.yml" up -d
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
# an existing database, including one created by an older checkout.
#
# init.sql gives up on a lock after 5 s (its lock_timeout), and a drain that is
# writing at that moment holds one. Every statement in it is idempotent, so a
# lock timeout is simply retried; any other error stops the install.
for attempt in 1 2 3 4 5; do
    if APPLY_ERR="$(docker exec -i claude-chats-db psql -q -v ON_ERROR_STOP=1 \
            -U claude -d claude_chats < "${ROOT_DIR}/init.sql" 2>&1 >/dev/null)"; then
        break
    fi
    if [[ "$APPLY_ERR" != *"lock timeout"* || $attempt -eq 5 ]]; then
        echo "$APPLY_ERR" >&2
        die "Applying init.sql failed."
    fi
    warn "init.sql waited too long for a lock (a drain is probably writing); retrying"
    sleep 2
done
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
uv sync --project "$HOOK_DIR"
ok "hook deps installed"

step "Installing MCP dependencies"
uv sync --project "$MCP_DIR"
ok "MCP deps installed"

# ---------------------------------------------------------------------------
# 5. Record the configuration
# ---------------------------------------------------------------------------
write_config
ok "Configuration written to ${CONFIG_FILE}"

# ---------------------------------------------------------------------------
# 6. Register the MCP server
# ---------------------------------------------------------------------------
step "Registering MCP server with Claude Code"

# Always re-register so env vars and the project path stay current — remove
# from all scopes first to avoid a stale entry in the other scope shadowing
# this one.
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
    -- uv run --project "$MCP_DIR" conversation-memory-mcp
ok "MCP 'conversation-memory' registered"

# ---------------------------------------------------------------------------
# 7. Configure the hooks
# ---------------------------------------------------------------------------
step "Configuring hooks in ${SETTINGS_FILE}"

# The hook touches nothing but local files, so it carries no provider or
# database settings: the drain it starts reads those from the config file.
# CLAUDE_CHATS_DRAIN_AFTER_CAPTURE is what makes it start one at all; the
# distributed edition leaves it unset, because its forwarder does the delivery.
HOOK_ENV="CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1"
[[ -n "${SSL_CERT_FILE:-}" ]] && HOOK_ENV+=" SSL_CERT_FILE=${SSL_CERT_FILE}"

HOOK_CMD="${HOOK_ENV} uv run --project \"${HOOK_DIR}\" record-conversation"

mkdir -p "$(dirname "$SETTINGS_FILE")"
[[ -f "$SETTINGS_FILE" ]] || echo '{}' > "$SETTINGS_FILE"

# Replacing every record-conversation entry is what keeps re-runs idempotent,
# and what moves an older install off whichever path it used to run from.
rewrite_hooks "$HOOK_CMD"
ok "Stop + UserPromptSubmit + SessionEnd hooks updated"

# ---------------------------------------------------------------------------
# 8. The drain schedule — the backstop for the drain the hook starts
# ---------------------------------------------------------------------------
UV_BIN="$(command -v uv)"
case "$(uname -s)" in
    Darwin)
        step "Installing the drain LaunchAgent"
        mkdir -p "$(dirname "$PLIST_DEST")"
        render_template "$PLIST_SRC" "$PLIST_DEST"
        plutil -lint "$PLIST_DEST" >/dev/null
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
        ok "Drainer runs every 2 minutes (log: /tmp/claude-chats-drain.log)"
        ;;
    Linux)
        step "Installing the drain systemd user timer"
        mkdir -p "$SYSTEMD_DEST"
        render_template "${SYSTEMD_SRC}/${SYSTEMD_UNIT}.service" "${SYSTEMD_DEST}/${SYSTEMD_UNIT}.service"
        cp "${SYSTEMD_SRC}/${SYSTEMD_UNIT}.timer" "${SYSTEMD_DEST}/${SYSTEMD_UNIT}.timer"
        if command -v systemctl &>/dev/null && systemctl --user daemon-reload 2>/dev/null; then
            systemctl --user enable --now "${SYSTEMD_UNIT}.timer"
            ok "Drainer runs every 2 minutes (log: journalctl --user -u ${SYSTEMD_UNIT})"
        else
            warn "No systemd user session; the units are in ${SYSTEMD_DEST} but not enabled."
            warn "The hook's own drain still runs; use ./install.sh --drain to catch up by hand."
        fi
        ;;
    *)
        warn "No drain schedule for $(uname -s). The hook's own drain still runs;"
        warn "use ./install.sh --drain to catch up anything it could not deliver."
        ;;
esac

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}All done!${NC}"
echo ""
echo "  Provider     : ${PROVIDER}"
echo "  Model        : ${MODEL}"
echo "  Dimensions   : ${DIMENSIONS}"
echo "  Database     : ${DB_URL}"
echo "  MCP tools    : search_memory · get_conversation · list_recent_sessions · recall_facts"
echo ""
echo "  Messages land in ${STATE_DIR}/outbox.db and reach Postgres within"
echo "  seconds. If one did not, force it with:  ./install.sh --drain"
if [[ -f "${STATE_DIR}/outbox-fallback.ndjson" ]]; then
    echo ""
    warn "${STATE_DIR}/outbox-fallback.ndjson exists: run ./install.sh --replay-fallback"
fi
echo ""
echo "  Stop the DB:      docker compose -f ${LOCALHOST_DIR}/docker-compose.yml down"
echo "  Restart Claude Code for the MCP server to appear."
