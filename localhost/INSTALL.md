# Install guide — single-machine edition

Everything here runs on one machine. Allow about ten minutes, most of which is
Docker pulling the PostgreSQL image and Ollama pulling an embedding model.

## 1. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| [Docker](https://docs.docker.com/get-docker/) | Runs PostgreSQL + pgvector | `docker ps` |
| [uv](https://docs.astral.sh/uv/) | Runs the Python hook and MCP server | `uv --version` |
| [Claude Code](https://claude.com/claude-code) | The thing being recorded | `claude --version` |
| Python 3.11+ | The hook and MCP server | `python3 --version` |
| An embedding provider | Turns messages into vectors | see below |

Pick one embedding provider:

- **Ollama** (default) — nothing leaves your machine. `brew install ollama`,
  then `ollama serve`. The installer pulls the model for you.
- **Amazon Bedrock** — uses your existing AWS credentials. No extra software.
- **OpenAI** — set `OPENAI_API_KEY`.

macOS and Linux are both supported. The drain's two-minute backstop is a
LaunchAgent on macOS and a systemd user timer on Linux.

## 2. Install

The installer lives in `localhost/`, and installs the hook and MCP server from
the repository root (`../hook`, `../mcp`), so run it from a full checkout:

```bash
cd claude-chats/localhost
./install.sh
```

To pick a provider other than Ollama:

```bash
CLAUDE_CHATS_PROVIDER=bedrock ./install.sh
CLAUDE_CHATS_PROVIDER=openai OPENAI_API_KEY=sk-... ./install.sh
```

The script:

1. checks the prerequisites;
2. starts the PostgreSQL container and applies `../init.sql` (idempotent, so this
   also picks up schema changes on an existing database);
3. pulls the Ollama model, if that is your provider;
4. creates the two virtualenvs (`../hook/.venv`, `../mcp/.venv`);
5. records your choices in `~/.claude-chats/config.env` (mode 600: it may hold
   an API key);
6. registers the `conversation-memory` MCP server with Claude Code;
7. adds the `Stop`, `UserPromptSubmit` and `SessionEnd` hooks to
   `~/.claude/settings.json`, with `CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1` so the
   hook starts a drain after each capture;
8. installs the drain schedule: a LaunchAgent on macOS, a systemd user timer on
   Linux.

Every step is idempotent. Re-run it as often as you like.

**Restart Claude Code** afterwards so it picks up the new MCP server.

### What the hook command carries, and what it does not

The hook entry in `settings.json` looks like this:

```
CLAUDE_CHATS_DRAIN_AFTER_CAPTURE=1 uv run --project "/path/to/claude-chats/hook" record-conversation
```

No database URL, provider or API key: the hook only ever writes local files.
The drain it starts reads those settings from `~/.claude-chats/config.env`, as
the timer's drain does. Without `CLAUDE_CHATS_DRAIN_AFTER_CAPTURE` the hook
never drains — that is how the distributed edition runs it, where a forwarder
owns the outbox.

## 3. Check it worked

Send a message in any Claude Code session. Then:

```bash
# should be 0 within a second or two: the hook's drain has delivered it
sqlite3 ~/.claude-chats/outbox.db \
  'SELECT count(*) FROM outbox WHERE sent_at IS NULL'

# the message should be in Postgres, with a vector
docker exec claude-chats-db psql -U claude -d claude_chats \
  -c 'SELECT count(*) AS messages, count(embedding) AS embedded FROM messages'
```

Then ask Claude to search its memory — it should call `search_memory` and find
the message you just sent.

## Maintenance

All of it hangs off the same script:

| Command | What it does |
|---|---|
| `./install.sh` | Install, upgrade, or pick up changed settings |
| `./install.sh --drain` | Drain the outbox into Postgres right now |
| `./install.sh --backfill` | Re-embed every message that has no vector, and rebuild the vector index |
| `./install.sh --replay-fallback` | Recover messages from the fallback file, then drain |
| `./install.sh --backup [path]` | `pg_dump` to a local file (default `~/.claude-chats/backups/`) |
| `./install.sh --restore [file]` | Restore a dump, then backfill the vectors |
| `./install.sh --uninstall` | Remove the hook, the MCP registration and the drain schedule |

Backups deliberately exclude the embedding vectors: they are incompressible, they
dominate the dump, and they are fully regenerable from message content. That is
why `--restore` runs a backfill at the end, and why it is the slow part.
`--backup` and `--restore` run the repository's `scripts/backup.sh --local` and
`scripts/restore.sh --local`, which can be run directly too — but keep the
`--local`: without it they default to S3, for the distributed edition's sake.
`--backup` keeps the newest 14 dumps in its directory and deletes older ones;
set `CLAUDE_CHATS_BACKUP_KEEP` to change that.

### Upgrading from before October 2026

Until October 2026 the single-machine edition carried its own copies of the
hook, the MCP server and the schema under `localhost/`, and offered two capture
modes, `direct` and `durable`. Now it runs the repository's shared `../hook`,
`../mcp` and `../init.sql`, and the outbox is the only capture mode. To upgrade,
pull an up-to-date checkout and re-run the installer from `localhost/`, with no
`--mode`:

```bash
cd claude-chats && git pull
cd localhost && ./install.sh
```

Do both together. Between the pull and the re-run, the hook entries and the
old LaunchAgent still name `localhost/hook`, which is now a stand-in with no code
that runs the shared hook instead: capture carries on into the outbox, and the
old LaunchAgent's drains fail harmlessly until the schema is upgraded, leaving
those messages pending for the new schedule. (Without that stand-in, the hook
would fail with an exit status Claude Code takes as "block", refusing every
prompt.) The stand-in resolves its dependencies the first time it runs, so it
wants the network then, as the pull did. The old MCP server registration does
not start in that gap.

The provider, model and their settings are kept from `~/.claude-chats/config.env`,
so you do not need to repeat them. The installer:

- rewrites the hook entries and the MCP registration, so nothing points at the
  old `localhost/hook` or `localhost/mcp` copies any more (those directories no
  longer exist). Hooks of your own, and the rest of `settings.json`, are left as
  they were; the previous file is kept as `settings.json.bak`;
- moves a `direct` install to the outbox, and drops the database and provider
  settings that mode put on the hook command line. `--mode direct` is refused;
- replaces the old LaunchAgent, which had the provider settings (an API key,
  for OpenAI) written into the plist, with one that reads them from
  `config.env`. On Linux, where the old edition scheduled nothing, it installs
  the systemd user timer;
- rewrites `config.env` without the old `CLAUDE_CHATS_CAPTURE_MODE` line, still
  mode 600;
- re-applies `../init.sql`, which brings the database to exactly the schema a new
  install gets: the `author`, `tool_name`, `model` and provenance columns, the
  BRIN index on `created_at`, and the facts tables. No row is changed. Messages
  recorded before the upgrade keep `author = 'unknown'`, rather than a guess;
- leaves the database volume, the outbox, the image archive and any fallback
  file exactly as they were.

Two things it points you at rather than doing itself:

- **Anything still pending in the outbox** (a `durable` install) is delivered
  by the new drain schedule: the LaunchAgent drains as soon as it is loaded,
  the systemd timer about a minute after it is enabled. To do it there and then, run
  `./install.sh --drain`.
- **A fallback file.** If `~/.claude-chats/outbox-fallback.ndjson` exists, the
  installer says so: run `./install.sh --replay-fallback`.

Restart Claude Code afterwards. From then on you have everything the main
edition has: messages typed while Claude is still working are captured
(HOME-411), each message records who wrote it, which tool produced it and which
model answered, `search_memory` takes a `since` window (3 months by default;
`since="all"` searches everything), its vector search uses 20 ivfflat probes,
and `recall_facts` is available. Sessions you started before the upgrade can be
resumed: the new hook adds to the same conversation without duplicating what
is already recorded.

Re-running `./install.sh` afterwards changes nothing. This whole path, from an
old install in each mode with real conversations in it, is tested by
`tests/test_localhost_upgrade.py` (CI runs it against a Postgres service, and
with real Docker Compose via `scripts/upgrade-check-localhost.sh`).

### Switching embedding provider

Re-run with the new provider, then re-embed everything:

```bash
CLAUDE_CHATS_PROVIDER=openai OPENAI_API_KEY=sk-... ./install.sh
./install.sh --backfill
```

`--backfill` only fills in *missing* vectors, so if you want the whole history
re-embedded under the new model you must clear the old ones first:

```bash
docker exec claude-chats-db psql -U claude -d claude_chats \
  -c 'UPDATE messages SET embedding = NULL'
./install.sh --backfill
```

Note that `init.sql` declares `vector(1024)`. A provider whose output is a
different width needs `CLAUDE_CHATS_DIMENSIONS` set to something it supports
(Titan v2: 256/512/1024; OpenAI 3-series: arbitrary), or a schema change.

## Troubleshooting

**Nothing is being recorded at all.**
Check the hook is registered: `grep -c record-conversation ~/.claude/settings.json`
should be 3 (`Stop`, `UserPromptSubmit`, `SessionEnd`). Hooks are read when
Claude Code starts, so restart it after an install.

**Messages are in the outbox but never reach Postgres.**
Read `~/.claude-chats/drain.log` (the hook's drains) and
`/tmp/claude-chats-drain.log` (the LaunchAgent's) or
`journalctl --user -u claude-chats-drain` (the systemd timer's). Then run
`./install.sh --drain` by hand — it prints the real error. The most common cause
is that Postgres is not running: `docker compose -f docker-compose.yml up -d`.
Each pending row also records its last error:
`sqlite3 ~/.claude-chats/outbox.db 'SELECT attempts, last_error FROM outbox WHERE sent_at IS NULL LIMIT 5'`.

**`~/.claude-chats/outbox-fallback.ndjson` exists.**
Something broke on the capture path and this is the safety net catching it.
Nothing is lost. Run `./install.sh --replay-fallback`, which replays the file
into the outbox, drains it, and archives the original with a timestamp.

**Messages are stored but `search_memory` finds nothing.**
Check for missing vectors:
```bash
docker exec claude-chats-db psql -U claude -d claude_chats \
  -c 'SELECT count(*) AS total, count(embedding) AS embedded FROM messages'
```
If `embedded` lags `total`, the provider was unavailable when those messages were
written. `./install.sh --backfill` fixes it.

**Ollama is not running.**
`ollama serve`, then `ollama list` to confirm the model is present. Messages
recorded while it was down keep their text and gain vectors on the next
`--backfill`.

**Port 5433 is already in use.**
Another PostgreSQL — quite possibly the distributed edition's, which uses the
same port and container name. Run one or the other, not both.

**`docker exec claude-chats-db` says no such container.**
`docker compose -f docker-compose.yml up -d` from this directory.

## Uninstalling

```bash
./install.sh --uninstall
```

That removes the hook entries, the MCP registration and the drain schedule, and
deliberately leaves your data alone. To remove that too:

```bash
docker compose -f docker-compose.yml down -v   # drops the database volume
rm -rf ~/.claude-chats                          # outbox, images, config, backups
```

## Where things live

| Path | What |
|---|---|
| `~/.claude-chats/outbox.db` | The SQLite outbox |
| `~/.claude-chats/outbox.db.drain.lock` | The lock concurrent drains take turns on |
| `~/.claude-chats/drain.log` | Output of the drains the hook starts (rolled over at 1 MB) |
| `~/.claude-chats/outbox-fallback.ndjson` | Last-resort capture when the outbox itself fails |
| `~/.claude-chats/images/` | Archived images with JSON sidecars |
| `~/.claude-chats/config.env` | What the installer recorded; read by every drain and the maintenance commands |
| `~/.claude-chats/backups/` | Default destination for `--backup` |
| `~/.claude/settings.json` | Where the hook is registered |
| `~/Library/LaunchAgents/com.nakomis.claude-chats-drain.plist` | The drain schedule (macOS) |
| `~/.config/systemd/user/claude-chats-drain.{service,timer}` | The drain schedule (Linux) |
| `/tmp/claude-chats-drain.log` | The LaunchAgent's drain output |
| Docker volume `localhost_pgdata` | The database itself |
