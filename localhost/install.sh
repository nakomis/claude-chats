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
# ── Claude Facts ──────────────────────────────────────────────────────────────
#
#   Every 15 minutes a separate LaunchAgent / systemd timer extracts durable
#   facts from new conversations with Claude Haiku (recall_facts finds them).
#   Never from the hook or the drain. See INSTALL.md, "Claude Facts".
#
#   FACTS_PROVIDER=claude-code  (default) — your own Claude Code login, run
#                     headlessly (`claude -p`); no API key. Counts towards
#                     your subscription's usage allowance.
#   FACTS_PROVIDER=anthropic    — ANTHROPIC_API_KEY must be set
#   FACTS_PROVIDER=bedrock      — your AWS credentials / profile
#                     (FACTS_MODEL default: global.anthropic.claude-haiku-5-5)
#   FACTS_PROVIDER=off          — no extraction
#
#   Only conversations from the moment facts were first installed are read.
#   Older history is opt-in: ./install.sh --facts-backfill [--since DATE].
#   FACTS_MAX_EXCHANGES  exchanges per 15-minute run (default: 10)
#
# ── Backups ───────────────────────────────────────────────────────────────────
#
#   Every night at 03:30 a third LaunchAgent / systemd timer runs
#   ../scripts/backup.sh into ~/.claude-chats/backups/: each complete month is
#   archived once (never pruned), and a nightly dump carries everything not yet
#   archived (the newest CLAUDE_CHATS_BACKUP_KEEP, default 14, are kept). The
#   same monthly-archive scheme the distributed edition runs on its server.
#   ./install.sh --backup takes one now; ./install.sh --restore puts it back.
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
FACTS_LABEL="com.nakomis.claude-chats-facts"
FACTS_PLIST_DEST="${HOME}/Library/LaunchAgents/${FACTS_LABEL}.plist"
FACTS_UNIT="claude-chats-facts"
FACTS_DIR="${ROOT_DIR}/localhost/facts"
BACKUP_LABEL="com.nakomis.claude-chats-nightly-backup"
BACKUP_PLIST_DEST="${HOME}/Library/LaunchAgents/${BACKUP_LABEL}.plist"
BACKUP_UNIT="claude-chats-nightly-backup"
OLD_BACKUP_PLIST="${HOME}/Library/LaunchAgents/com.nakomis.claude-chats-backup.plist"
DEFAULT_FACTS_MAX_EXCHANGES=10
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
  ./install.sh --backup [dir]      back up now, as the nightly schedule does
                                   (default dir: ~/.claude-chats/backups)
  ./install.sh --restore [dir|file]
                                   restore the newest nightly and every archived
                                   month before it (or a named nightly)
  ./install.sh --uninstall         remove the hook, MCP, drain, facts and backup
                                   schedules

Claude Facts:
  ./install.sh --facts             run one fact extraction now
  ./install.sh --facts-backfill [--since YYYY-MM-DD]
                                   also extract from older history (default: all
                                   of it), at FACTS_MAX_EXCHANGES per run
  ./install.sh --facts-backfill-stop
                                   back to conversations since install only
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
        esac
        if [[ "$PROVIDER" == "bedrock" || "$FACTS_PROVIDER" == "bedrock" ]]; then
            [[ -n "${AWS_PROFILE:-}" ]] && echo "AWS_PROFILE=${AWS_PROFILE}"
            [[ -n "${AWS_REGION:-}"  ]] && echo "AWS_REGION=${AWS_REGION}"
            [[ -n "${AWS_DEFAULT_REGION:-}" ]] && echo "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}"
        fi
        # Claude Facts. FACTS_PROVIDER is what runs; FACTS_PROVIDER_REQUESTED what
        # was asked for, so a re-run after logging in turns it back on.
        echo "FACTS_PROVIDER=${FACTS_PROVIDER}"
        echo "FACTS_PROVIDER_REQUESTED=${FACTS_PROVIDER_REQUESTED}"
        [[ -n "${FACTS_INSTALLED_AT:-}" ]] && echo "FACTS_INSTALLED_AT=${FACTS_INSTALLED_AT}"
        [[ -n "${FACTS_SINCE:-}" ]] && echo "FACTS_SINCE=${FACTS_SINCE}"
        echo "FACTS_MAX_EXCHANGES=${FACTS_MAX_EXCHANGES}"
        [[ -n "${FACTS_MODEL:-}" ]] && echo "FACTS_MODEL=${FACTS_MODEL}"
        [[ -n "${FACTS_CLAUDE_BIN:-}" ]] && echo "FACTS_CLAUDE_BIN=${FACTS_CLAUDE_BIN}"
        [[ "$FACTS_PROVIDER" == "anthropic" ]] && echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
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

# Set one key in the config file in place (the backfill switches), keeping the
# rest and the file's permissions.
config_set() {
    [[ -f "$CONFIG_FILE" ]] || die "No ${CONFIG_FILE}: run ./install.sh first."
    python3 - "$CONFIG_FILE" "$1" "$2" <<'PYEOF'
import os, sys

path, key, value = sys.argv[1:4]
lines = [l for l in open(path).read().splitlines() if not l.startswith(key + "=")]
lines.append(f"{key}={value}")
tmp = f"{path}.tmp.{os.getpid()}"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, path)
PYEOF
}

# Run one facts service run with the recorded configuration, as the timer does.
run_facts() {
    (cd "$ROOT_DIR" && uv run --project "$FACTS_DIR" python -m facts.single_machine \
        --env-file "$CONFIG_FILE" "$@")
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

# Remove one schedule: a LaunchAgent label on macOS, a systemd unit pair on Linux.
remove_one_schedule() {
    local label="$1" unit="$2"
    case "$(uname -s)" in
        Darwin)
            launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
            rm -f "${HOME}/Library/LaunchAgents/${label}.plist"
            ;;
        Linux)
            if command -v systemctl &>/dev/null; then
                systemctl --user disable --now "${unit}.timer" 2>/dev/null || true
            fi
            rm -f "${SYSTEMD_DEST}/${unit}.service" "${SYSTEMD_DEST}/${unit}.timer"
            if command -v systemctl &>/dev/null; then
                systemctl --user daemon-reload 2>/dev/null || true
            fi
            ;;
    esac
}

remove_schedule() {
    remove_one_schedule "$LABEL" "$SYSTEMD_UNIT"
    remove_one_schedule "$FACTS_LABEL" "$FACTS_UNIT"
    remove_one_schedule "$BACKUP_LABEL" "$BACKUP_UNIT"
}

# Is `claude` logged in to an account (not just holding an API key)? Asked the
# way the facts schedule will run it: no API key (the extractor removes it), and
# the near-empty environment of launchd or systemd, with only claude's own
# directory added to the PATH (which is what facts.single_machine does), so a
# claude that needs `node` from beside it is caught here rather than every 15
# minutes in a log.
claude_logged_in() {
    local status
    status="$(env -i HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-}" TMPDIR="${TMPDIR:-/tmp}" \
        PATH="$(dirname "$1"):/usr/bin:/bin:/usr/sbin:/sbin" "$1" auth status --json 2>/dev/null)" || return 1
    python3 -c 'import json, sys; sys.exit(0 if json.loads(sys.argv[1]).get("loggedIn") is True else 1)' \
        "$status" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

ACTION="install"
EXTRA_ARG=""
BACKFILL_SINCE=""

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
        --facts)           ACTION="facts"; shift ;;
        --facts-backfill)  ACTION="facts-backfill"; shift ;;
        --facts-backfill-stop) ACTION="facts-backfill-stop"; shift ;;
        --since)           BACKFILL_SINCE="${2:-}"; [[ -n "$BACKFILL_SINCE" ]] || die "--since needs a date (YYYY-MM-DD)."; shift 2 ;;
        --since=*)         BACKFILL_SINCE="${1#*=}"; [[ -n "$BACKFILL_SINCE" ]] || die "--since needs a date (YYYY-MM-DD)."; shift ;;
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
if [[ -n "$BACKFILL_SINCE" && "$ACTION" != "facts-backfill" ]]; then
    die "--since only goes with --facts-backfill."
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
        exec "${ROOT_DIR}/scripts/backup.sh" ${EXTRA_ARG:+"$EXTRA_ARG"}
        ;;
    restore)
        # The backfill at the end of a restore embeds with the installed provider.
        config_env
        exec env ${CONFIG_ENV[@]+"${CONFIG_ENV[@]}"} \
            "${ROOT_DIR}/scripts/restore.sh" --local ${EXTRA_ARG:+"$EXTRA_ARG"}
        ;;
    facts)
        step "Extracting facts now"
        run_facts
        exit $?
        ;;
    facts-backfill)
        [[ "$(config_get FACTS_PROVIDER)" =~ ^(claude-code|anthropic|bedrock)$ ]] \
            || die "Claude Facts is off. Turn it on first, e.g. FACTS_PROVIDER=claude-code ./install.sh"
        if [[ -z "$BACKFILL_SINCE" || "$BACKFILL_SINCE" == "all" ]]; then
            BACKFILL_SINCE="all"
        else
            python3 -c 'import sys; from datetime import datetime; datetime.fromisoformat(sys.argv[1])' \
                "$BACKFILL_SINCE" 2>/dev/null || die "--since '${BACKFILL_SINCE}' is not a date (YYYY-MM-DD)."
        fi
        config_set FACTS_SINCE "$BACKFILL_SINCE"
        RATE="$(config_get FACTS_MAX_EXCHANGES)"
        ok "Backfill on: facts from ${BACKFILL_SINCE/#all/all of your history} onwards"
        echo "  At most ${RATE:-$DEFAULT_FACTS_MAX_EXCHANGES} exchanges per 15-minute run (FACTS_MAX_EXCHANGES), new"
        echo "  conversations first. Progress is kept, so stopping and starting loses nothing."
        echo "  Stop it with:  ./install.sh --facts-backfill-stop"
        exit 0
        ;;
    facts-backfill-stop)
        INSTALLED_AT="$(config_get FACTS_INSTALLED_AT)"
        [[ -n "$INSTALLED_AT" ]] || die "Claude Facts has not been installed: nothing to stop."
        config_set FACTS_SINCE "$INSTALLED_AT"
        ok "Backfill stopped: back to conversations since ${INSTALLED_AT}"
        exit 0
        ;;
    uninstall)
        step "Removing the drain, facts and backup schedules"
        remove_schedule
        ok "Schedules removed"

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

# Claude Facts: the same order. What was asked for is remembered separately from
# what runs, so an install that fell back to off retries the choice next time.
FACTS_PROVIDER_REQUESTED="${FACTS_PROVIDER:-$(config_get FACTS_PROVIDER_REQUESTED)}"
FACTS_PROVIDER_REQUESTED="${FACTS_PROVIDER_REQUESTED:-$(config_get FACTS_PROVIDER)}"
FACTS_PROVIDER_REQUESTED="${FACTS_PROVIDER_REQUESTED:-claude-code}"
FACTS_PROVIDER="$FACTS_PROVIDER_REQUESTED"
FACTS_MAX_EXCHANGES="${FACTS_MAX_EXCHANGES:-$(config_get FACTS_MAX_EXCHANGES)}"
FACTS_MAX_EXCHANGES="${FACTS_MAX_EXCHANGES:-$DEFAULT_FACTS_MAX_EXCHANGES}"
[[ "$FACTS_MAX_EXCHANGES" =~ ^[1-9][0-9]*$ ]] || die "FACTS_MAX_EXCHANGES must be a whole number above 0."
# A remembered model only belongs to the provider it was given for: a Bedrock
# inference profile passed to `claude --model` would fail every run.
if [[ -z "${FACTS_MODEL:-}" && "$(config_get FACTS_PROVIDER_REQUESTED)" == "$FACTS_PROVIDER_REQUESTED" ]]; then
    FACTS_MODEL="$(config_get FACTS_MODEL)"
fi
# Never from the environment: FACTS_SINCE is set once, when facts are first
# installed, and afterwards only by --facts-backfill / --facts-backfill-stop.
FACTS_INSTALLED_AT="$(config_get FACTS_INSTALLED_AT)"
FACTS_SINCE="$(config_get FACTS_SINCE)"
if [[ "$FACTS_PROVIDER" == "anthropic" ]]; then
    ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(config_get ANTHROPIC_API_KEY)}"
fi
if [[ "$FACTS_PROVIDER" == "bedrock" && "$PROVIDER" != "bedrock" ]]; then
    AWS_PROFILE="${AWS_PROFILE:-$(config_get AWS_PROFILE)}"
    AWS_REGION="${AWS_REGION:-$(config_get AWS_REGION)}"
    AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(config_get AWS_DEFAULT_REGION)}"
fi

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

# Claude Facts: anything missing turns extraction off with a warning; it never
# stops the install (capture and search do not need it).
facts_off() {
    warn "Claude Facts is off: $*"
    FACTS_PROVIDER="off"
}
FACTS_CLAUDE_BIN=""
case "$FACTS_PROVIDER" in
    claude-code)
        # The absolute path: launchd and systemd run with a PATH that may not have it.
        FACTS_CLAUDE_BIN="$(command -v claude)"
        if claude_logged_in "$FACTS_CLAUDE_BIN"; then
            ok "Claude Facts: claude-code (your Claude Code login)"
        else
            facts_off "\`claude auth status\` says Claude Code is not logged in to an account (or it
   cannot run from a scheduled job's environment).
   Log in (run \`claude\`, then /login), then re-run ./install.sh to turn it on."
        fi
        ;;
    anthropic)
        if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
            ok "Claude Facts: anthropic (ANTHROPIC_API_KEY is set)"
        else
            facts_off "FACTS_PROVIDER=anthropic needs ANTHROPIC_API_KEY. Set it and re-run ./install.sh."
        fi
        ;;
    bedrock)
        if [[ -z "${AWS_PROFILE:-}" && -z "${AWS_ACCESS_KEY_ID:-}" && ! -f "${HOME}/.aws/credentials" && ! -f "${HOME}/.aws/config" ]]; then
            facts_off "FACTS_PROVIDER=bedrock found no AWS credentials. Configure AWS_PROFILE and re-run ./install.sh."
        else
            ok "Claude Facts: bedrock (${FACTS_MODEL:-global.anthropic.claude-haiku-5-5}; needs model access in your account)"
        fi
        ;;
    off)
        ok "Claude Facts: off"
        ;;
    *)
        die "Unknown FACTS_PROVIDER '${FACTS_PROVIDER}'. Expected claude-code, anthropic, bedrock or off."
        ;;
esac
if [[ "$FACTS_PROVIDER" != "off" && -z "$FACTS_SINCE" ]]; then
    # First time facts are on: only conversations from now are read.
    FACTS_INSTALLED_AT="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
    FACTS_SINCE="$FACTS_INSTALLED_AT"
fi

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

step "Installing Claude Facts dependencies"
uv sync --project "$FACTS_DIR"
ok "facts deps installed"

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
# 9. The Claude Facts schedule — every 15 minutes, never from the hook or drain
# ---------------------------------------------------------------------------
if [[ "$FACTS_PROVIDER" == "off" ]]; then
    remove_one_schedule "$FACTS_LABEL" "$FACTS_UNIT"
else
    case "$(uname -s)" in
        Darwin)
            step "Installing the Claude Facts LaunchAgent"
            render_template "${LOCALHOST_DIR}/launchd/${FACTS_LABEL}.plist" "$FACTS_PLIST_DEST"
            plutil -lint "$FACTS_PLIST_DEST" >/dev/null
            launchctl bootout "gui/$(id -u)/${FACTS_LABEL}" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" "$FACTS_PLIST_DEST"
            ok "Facts are extracted every 15 minutes (log: /tmp/claude-chats-facts.log)"
            ;;
        Linux)
            step "Installing the Claude Facts systemd user timer"
            mkdir -p "$SYSTEMD_DEST"
            render_template "${SYSTEMD_SRC}/${FACTS_UNIT}.service" "${SYSTEMD_DEST}/${FACTS_UNIT}.service"
            cp "${SYSTEMD_SRC}/${FACTS_UNIT}.timer" "${SYSTEMD_DEST}/${FACTS_UNIT}.timer"
            if command -v systemctl &>/dev/null && systemctl --user daemon-reload 2>/dev/null; then
                systemctl --user enable --now "${FACTS_UNIT}.timer"
                ok "Facts are extracted every 15 minutes (log: journalctl --user -u ${FACTS_UNIT})"
            else
                warn "No systemd user session; the facts units are in ${SYSTEMD_DEST} but not enabled."
                warn "Run ./install.sh --facts to extract by hand."
            fi
            ;;
        *)
            warn "No facts schedule for $(uname -s); run ./install.sh --facts to extract by hand."
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# 10. The nightly backup — local disk, monthly archive + nightly dump (CHATS-41)
# ---------------------------------------------------------------------------
case "$(uname -s)" in
    Darwin)
        step "Installing the nightly backup LaunchAgent"
        render_template "${LOCALHOST_DIR}/launchd/${BACKUP_LABEL}.plist" "$BACKUP_PLIST_DEST"
        plutil -lint "$BACKUP_PLIST_DEST" >/dev/null
        launchctl bootout "gui/$(id -u)/${BACKUP_LABEL}" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$BACKUP_PLIST_DEST"
        ok "Backed up nightly at 03:30 to ${STATE_DIR}/backups (log: /tmp/claude-chats-backup.log)"
        ;;
    Linux)
        step "Installing the nightly backup systemd user timer"
        mkdir -p "$SYSTEMD_DEST"
        render_template "${SYSTEMD_SRC}/${BACKUP_UNIT}.service" "${SYSTEMD_DEST}/${BACKUP_UNIT}.service"
        cp "${SYSTEMD_SRC}/${BACKUP_UNIT}.timer" "${SYSTEMD_DEST}/${BACKUP_UNIT}.timer"
        if command -v systemctl &>/dev/null && systemctl --user daemon-reload 2>/dev/null; then
            systemctl --user enable --now "${BACKUP_UNIT}.timer"
            ok "Backed up nightly at 03:30 to ${STATE_DIR}/backups (log: journalctl --user -u ${BACKUP_UNIT})"
        else
            warn "No systemd user session; the backup units are in ${SYSTEMD_DEST} but not enabled."
            warn "Run ./install.sh --backup to back up by hand."
        fi
        ;;
    *)
        warn "No backup schedule for $(uname -s); run ./install.sh --backup to back up by hand."
        ;;
esac
# The retired Mac-to-S3 agent; scripts/backup.sh refuses to run while it is
# installed, so say so here, where someone is watching.
if [[ -f "$OLD_BACKUP_PLIST" ]]; then
    warn "${OLD_BACKUP_PLIST} is the retired Mac-to-S3 backup agent, and backups"
    warn "refuse to run while it is installed. Remove it:"
    warn "  launchctl bootout gui/\$(id -u)/com.nakomis.claude-chats-backup; rm ${OLD_BACKUP_PLIST}"
fi

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
echo "  Backups      : ${STATE_DIR}/backups, nightly at 03:30"
echo "  Claude Facts : ${FACTS_PROVIDER}${FACTS_SINCE:+ (conversations since ${FACTS_SINCE}, ${FACTS_MAX_EXCHANGES} exchanges per run)}"
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
