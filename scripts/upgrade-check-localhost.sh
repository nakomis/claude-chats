#!/usr/bin/env bash
# Upgrade check of the single-machine edition, with real Docker (CHATS-37).
#
#   scripts/upgrade-check-localhost.sh
#
# Runs tests/test_localhost_upgrade.py with CLAUDE_CHATS_UPGRADE_DOCKER=1: the
# old (pre-October 2026) localhost/install.sh brings up its own claude-chats-db
# container with `docker compose`, captures conversations through the old hook,
# and then the current install.sh upgrades it in place. Neither installer is
# modified. Everything else (HOME, claude, launchctl, systemctl, the embedder)
# is a throwaway or a stub, as in the default run of that test.
#
# The test's default run, in CI's root suite, does the same against a pgvector
# service container instead of Docker Compose; this is the full variant, and
# CI runs it too.
#
# It refuses to start if a container called claude-chats-db exists, or port
# 5433 is taken, because both are what a real install uses: never run the old
# installer on a machine where you keep your conversations unless you have
# stopped and removed that container yourself, and know where your volume is.
# The compose project is claude-chats-upgrade-check, so its volume is
# claude-chats-upgrade-check_pgdata and a real localhost_pgdata is never used.
# Needs Docker, uv and a full git clone (the old installer comes from history).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export COMPOSE_PROJECT_NAME="claude-chats-upgrade-check"

die() { echo "✗ $*" >&2; exit 1; }

docker info >/dev/null 2>&1 || die "Docker is not running."
if [[ -n "$(docker ps -aq --filter 'name=^claude-chats-db$')" ]]; then
    die "A claude-chats-db container exists; it may hold real conversations. Refusing."
fi
if (exec 3<>/dev/tcp/127.0.0.1/5433) 2>/dev/null; then
    die "Something is listening on port 5433 (a real install?). Refusing."
fi

cleanup() {
    # Only ever ours: the checks above found no such container at the start.
    docker rm -f claude-chats-db >/dev/null 2>&1 || true
    docker volume rm -f "${COMPOSE_PROJECT_NAME}_pgdata" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT"
CLAUDE_CHATS_UPGRADE_DOCKER=1 uv run --no-project --with pytest --with 'psycopg[binary]' \
    pytest tests/test_localhost_upgrade.py -v -rs
