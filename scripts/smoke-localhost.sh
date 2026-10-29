#!/usr/bin/env bash
# Smoke test of the single-machine edition, through its real entry points.
#
#   CLAUDE_CHATS_TEST_ADMIN_DSN=postgresql://… scripts/smoke-localhost.sh
#
# Creates a scratch database on that server, applies init.sql to it, runs the
# hook's `record-conversation` on a fixture transcript under a throwaway HOME
# (set up as localhost/install.sh would leave it, with the drain-after-capture
# switch on), runs `drain-outbox` as the timer would, checks the rows in
# Postgres, then asks the MCP server's search_memory to find them. Embeddings
# come from a fake Ollama (hook/tests/support.py), so no model and no network.
# The scratch database is dropped at the end, pass or fail.
#
# CI runs this against a pgvector service container. Needs uv and psql.
set -euo pipefail

: "${CLAUDE_CHATS_TEST_ADMIN_DSN:?set it to a Postgres (with pgvector) where the role can CREATE DATABASE}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
DB="claude_chats_smoke_$$"
ADMIN="$CLAUDE_CHATS_TEST_ADMIN_DSN"
OLLAMA_PID=""
export PGOPTIONS="--client-min-messages=warning"  # init.sql re-applied is all NOTICEs

cleanup() {
    [[ -n "$OLLAMA_PID" ]] && kill "$OLLAMA_PID" 2>/dev/null || true
    psql "$ADMIN" -q -c "DROP DATABASE IF EXISTS ${DB} WITH (FORCE)" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

step() { echo; echo "▶ $*"; }

# The scratch database's DSN: the admin DSN with the database name swapped.
DSN="$(uv run --project "${ROOT}/hook" python -c '
import sys, psycopg
info = psycopg.conninfo.conninfo_to_dict(sys.argv[1]); info["dbname"] = sys.argv[2]
print(psycopg.conninfo.make_conninfo(**info))' "$ADMIN" "$DB")"

step "Creating ${DB} and applying init.sql"
psql "$ADMIN" -q -c "CREATE DATABASE ${DB}"
psql "$DSN" -q -v ON_ERROR_STOP=1 -f "${ROOT}/init.sql" >/dev/null
# Twice: install.sh applies it on every run.
psql "$DSN" -q -v ON_ERROR_STOP=1 -f "${ROOT}/init.sql" >/dev/null

step "Starting a fake Ollama"
uv run --project "${ROOT}/hook" python -c '
import sys, time
sys.path.insert(0, sys.argv[1])
from support import fake_ollama
with fake_ollama() as (url, _):
    open(sys.argv[2], "w").write(url)
    while True:
        time.sleep(3600)' "${ROOT}/hook/tests" "${WORK}/ollama-url" &
OLLAMA_PID=$!
for _ in $(seq 1 100); do [[ -s "${WORK}/ollama-url" ]] && break; sleep 0.1; done
OLLAMA_URL="$(cat "${WORK}/ollama-url")"

# The hook and the drain see this as HOME; nothing else does, so the tools
# running the test keep their own.
FAKE_HOME="${WORK}/home"
STATE="${FAKE_HOME}/.claude-chats"
mkdir -p "$STATE"
cat > "${STATE}/config.env" <<EOF
CLAUDE_CHATS_PROVIDER=ollama
CLAUDE_CHATS_MODEL=mxbai-embed-large
CLAUDE_CHATS_DB_URL=${DSN}
OLLAMA_BASE_URL=${OLLAMA_URL}
EOF

cat > "${WORK}/transcript.jsonl" <<'EOF'
{"uuid": "smoke-u1", "timestamp": "2026-10-20T10:00:01+00:00", "message": {"role": "user", "content": "How do I descale the espresso machine in the workshop?"}}
{"uuid": "smoke-a1", "timestamp": "2026-10-20T10:00:02+00:00", "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Run citric acid through the espresso machine, then flush twice."}]}}
{"type": "custom-title", "customTitle": "Espresso descaling"}
EOF

step "Running the hook (record-conversation), as Claude Code would"
echo "{\"session_id\": \"smoke-session\", \"transcript_path\": \"${WORK}/transcript.jsonl\", \"cwd\": \"/w\", \"git_branch\": \"main\"}" \
    | HOME="$FAKE_HOME" CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1 uv run --project "${ROOT}/hook" record-conversation

step "Running drain-outbox, as the timer would"
HOME="$FAKE_HOME" uv run --project "${ROOT}/hook" drain-outbox --env-file "${STATE}/config.env"

step "Checking the rows"
# The hook's own background drain may still be finishing; give it a moment.
for _ in $(seq 1 50); do
    COUNT="$(psql "$DSN" -At -c "SELECT count(*) FROM messages WHERE embedding IS NOT NULL")"
    [[ "$COUNT" == 2 ]] && break
    sleep 0.2
done
psql "$DSN" -c "SELECT m.message_uuid, m.author, m.model, m.embedding IS NOT NULL AS embedded, c.name
                FROM messages m JOIN conversations c ON c.id = m.conversation_id ORDER BY sequence_num"
[[ "$COUNT" == 2 ]] || { echo "expected 2 embedded messages, found ${COUNT}" >&2; exit 1; }
AUTHORS="$(psql "$DSN" -At -c "SELECT string_agg(author, ',' ORDER BY sequence_num) FROM messages")"
[[ "$AUTHORS" == "martin,claude" ]] || { echo "authors were ${AUTHORS}" >&2; exit 1; }
PENDING="$(uv run --project "${ROOT}/hook" python -c 'import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0])' \
    "${STATE}/outbox.db")"
[[ "$PENDING" == 0 ]] || { echo "${PENDING} row(s) still pending in the outbox" >&2; exit 1; }

step "Searching through the MCP server"
CLAUDE_CHATS_DB_URL="$DSN" OLLAMA_BASE_URL="$OLLAMA_URL" CLAUDE_CHATS_IVFFLAT_PROBES=100 \
    uv run --project "${ROOT}/mcp" python -c '
from conversation_memory_mcp.server import search_memory
out = search_memory("descale espresso machine", since="all")
print(out["status"], [(r["session_id"], r["name"]) for r in out["results"]])
assert out["results"] and out["results"][0]["session_id"] == "smoke-session", out
assert out["results"][0]["name"] == "Espresso descaling", out
'

echo
echo "✓ single-machine smoke test passed"
