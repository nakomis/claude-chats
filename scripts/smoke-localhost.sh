#!/usr/bin/env bash
# Smoke test of the single-machine edition, through its real entry points.
#
#   CLAUDE_CHATS_TEST_ADMIN_DSN=postgresql://… scripts/smoke-localhost.sh
#
# Creates a scratch database on that server, applies init.sql to it, runs the
# hook's `record-conversation` on a fixture transcript under a throwaway HOME
# (set up as localhost/install.sh would leave it, with the drain-after-capture
# switch on), runs `drain-outbox` as the timer would, checks the rows in
# Postgres, then asks the MCP server's search_memory to find them. Then it runs
# one Claude Facts extraction as the facts timer would (CHATS-39), with a fake
# `claude` that answers as `claude -p --json-schema` does, and asks recall_facts
# for the fact it extracted. Embeddings come from a fake Ollama
# (hook/tests/support.py), so no model and no network.
# The scratch database is dropped at the end, pass or fail.
#
# CI runs this against a pgvector service container. Needs uv and psql.
set -euo pipefail

# Nothing from the caller's shell decides which provider, database or replica
# the hook, the drain and the MCP server use: only what this script sets.
while IFS= read -r var; do unset "$var"; done < <(compgen -e | grep '^CLAUDE_CHATS_' | grep -v '^CLAUDE_CHATS_TEST_ADMIN_DSN$')

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
# A fake Claude Code for the facts extractor: records how it was run, and
# answers as `claude -p --output-format json --json-schema` does.
mkdir -p "${WORK}/bin"
cat > "${WORK}/bin/claude" <<'FAKE'
#!/usr/bin/env bash
{ echo "argv: $*"; echo "api-key: ${ANTHROPIC_API_KEY:-absent}"; echo "cwd: $PWD"; } >> "${FAKE_CLAUDE_LOG}"
cat > /dev/null
cat <<'JSON'
{"type": "result", "subtype": "success", "is_error": false, "stop_reason": "tool_use",
 "terminal_reason": "completed", "api_error_status": null, "num_turns": 2, "result": "",
 "structured_output": {"memory": [{"id": "0", "text": "The workshop espresso machine is descaled with citric acid",
   "kind": "config", "subject": "espresso machine", "attribute": "descaling"}]},
 "usage": {"input_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 4600, "output_tokens": 60}}
JSON
FAKE
chmod +x "${WORK}/bin/claude"

cat > "${STATE}/config.env" <<EOF
CLAUDE_CHATS_PROVIDER=ollama
CLAUDE_CHATS_MODEL=mxbai-embed-large
CLAUDE_CHATS_DB_URL=${DSN}
OLLAMA_BASE_URL=${OLLAMA_URL}
FACTS_PROVIDER=claude-code
FACTS_PROVIDER_REQUESTED=claude-code
FACTS_INSTALLED_AT=2026-10-01T00:00:00+00:00
FACTS_SINCE=2026-10-01T00:00:00+00:00
FACTS_MAX_EXCHANGES=10
FACTS_CLAUDE_BIN=${WORK}/bin/claude
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

step "Extracting facts as the facts timer would (fake claude -p)"
# The unit's command, from the checkout's root, with next to nothing in the
# environment but an API key that must never reach claude.
(cd "$ROOT" && env -i HOME="$FAKE_HOME" PATH="$PATH" ANTHROPIC_API_KEY=must-not-be-used \
    FAKE_CLAUDE_LOG="${WORK}/claude.log" UV_CACHE_DIR="$(uv cache dir)" \
    uv run --project "${ROOT}/localhost/facts" python -m facts.single_machine --env-file "${STATE}/config.env")
cat "${WORK}/claude.log"
grep -q "^api-key: absent$" "${WORK}/claude.log" || { echo "claude -p saw an API key" >&2; exit 1; }
for flag in "-p" "--strict-mcp-config" "--no-session-persistence" "--json-schema" "disableAllHooks"; do
    grep -q -- "$flag" "${WORK}/claude.log" || { echo "claude -p was run without ${flag}" >&2; exit 1; }
done
if grep -q -- "--bare" "${WORK}/claude.log"; then echo "claude -p was run with --bare" >&2; exit 1; fi
psql "$DSN" -c "SELECT text, kind, embedder, embedding IS NOT NULL AS embedded, extractor FROM facts"
FACT="$(psql "$DSN" -At -c "SELECT embedder || '|' || (embedding IS NOT NULL) FROM facts")"
[[ "$FACT" == "ollama-mxbai-embed-large|true" ]] \
    || { echo "expected one fact, embedded by the hook's provider; got ${FACT}" >&2; exit 1; }
DONE="$(psql "$DSN" -At -c "SELECT p.last_seq FROM facts_progress p
    JOIN conversations c ON c.id = p.conversation_id WHERE c.session_id = 'smoke-session'")"
[[ -n "$DONE" ]] || { echo "facts_progress did not advance" >&2; exit 1; }

step "Recalling the extracted fact through the MCP server (one database, no replica)"
# Started without CLAUDE_CHATS_READ_DB_URL, as install.sh registers it.
CLAUDE_CHATS_DB_URL="$DSN" OLLAMA_BASE_URL="$OLLAMA_URL" \
    uv run --project "${ROOT}/mcp" python -c '
from conversation_memory_mcp.server import READ_DB_URL, recall_facts
assert READ_DB_URL is None, READ_DB_URL
text = "The workshop espresso machine is descaled with citric acid"
# "garage" is in no fact and full-text needs every word: only the vector
# half can find it, so this proves the fact was embedded as queries are.
out = recall_facts("how is the espresso machine descaled in the garage")
print(out["status"], out["read_from"], [r["text"] for r in out["results"]])
assert out["read_from"] == "primary", out
assert out["results"] and out["results"][0]["text"] == text, out
assert out["results"][0]["source"]["session_id"] == "smoke-session", out
'

echo
echo "✓ single-machine smoke test passed"
