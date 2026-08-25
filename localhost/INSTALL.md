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

On Linux everything works except the drainer's schedule, which uses launchd.
See [Running the drainer on Linux](#running-the-drainer-on-linux).

## 2. Choose a capture mode

The installer asks. If you would rather decide now:

|  | **direct** | **durable** |
|---|---|---|
| The hook | embeds and writes to Postgres inline | appends to a local SQLite outbox |
| Searchable | immediately | within ~2 minutes |
| Moving parts | hook, Postgres, provider | hook, outbox, drainer, Postgres, provider |
| If Postgres is down | message goes to a fallback file, needs `--replay-fallback` | message waits in the outbox, drains by itself |
| If the provider is down | message stored, vector missing (fix with `--backfill`) | same |
| Background process | none | a LaunchAgent every 2 minutes |

**Pick `durable` if you are unsure.** It is the default, and it is what the
distributed edition does, for the reason recorded in `hook/hook/record.py`: the
original direct implementation failed *silently* whenever Docker was down, and a
conversation you never notice losing is worse than one that arrives late.

**Pick `direct` if** you want the fewest possible moving parts, you keep Docker
running anyway, and you would rather see a message in the database the instant a
session ends.

You can change your mind at any time — see [Switching mode](#switching-mode).

## 3. Install

```bash
cd localhost
./install.sh
```

To skip the prompt, or to install non-interactively:

```bash
./install.sh --mode durable
./install.sh --mode direct
```

To pick a provider other than Ollama:

```bash
CLAUDE_CHATS_PROVIDER=bedrock ./install.sh
CLAUDE_CHATS_PROVIDER=openai OPENAI_API_KEY=sk-... ./install.sh
```

The script:

1. checks the prerequisites;
2. starts the PostgreSQL container and applies `init.sql` (idempotent, so this
   also picks up schema changes on an existing database);
3. pulls the Ollama model, if that is your provider;
4. creates the two virtualenvs;
5. records your choices in `~/.claude-chats/config.env`;
6. registers the `conversation-memory` MCP server with Claude Code;
7. adds the `Stop` and `UserPromptSubmit` hooks to `~/.claude/settings.json`;
8. in durable mode, installs the drain LaunchAgent.

Every step is idempotent. Re-run it as often as you like.

**Restart Claude Code** afterwards so it picks up the new MCP server.

## 4. Check it worked

Send a message in any Claude Code session and stop the session. Then:

```bash
# durable mode: the message is in the outbox, and drains within ~2 minutes
sqlite3 ~/.claude-chats/outbox.db \
  'SELECT count(*) FROM outbox WHERE sent_at IS NULL'

# force the drain rather than waiting
./install.sh --drain

# either mode: the message should now be in Postgres, with a vector
docker exec claude-chats-db psql -U claude -d claude_chats \
  -c 'SELECT count(*) AS messages, count(embedding) AS embedded FROM messages'
```

Then ask Claude to search its memory — it should call `search_memory` and find
the message you just sent.

## Maintenance

All of it hangs off the same script:

| Command | What it does |
|---|---|
| `./install.sh --mode direct\|durable` | Install, or switch capture mode |
| `./install.sh --drain` | Drain the outbox into Postgres right now |
| `./install.sh --backfill` | Re-embed every message that has no vector, and rebuild the vector index |
| `./install.sh --replay-fallback` | Recover messages from the fallback file, then drain |
| `./install.sh --backup [path]` | `pg_dump` to a local file (default `~/.claude-chats/backups/`) |
| `./install.sh --restore [file]` | Restore a dump, then backfill the vectors |
| `./install.sh --uninstall` | Remove the hook, the MCP registration and the LaunchAgent |

Backups deliberately exclude the embedding vectors: they are incompressible, they
dominate the dump, and they are fully regenerable from message content. That is
why `--restore` runs a backfill at the end, and why it is the slow part.

### Switching mode

```bash
./install.sh --mode direct
```

Switching **durable → direct** drains the outbox first, because once direct mode
is in force nothing else will. If the drain fails the switch is refused rather
than stranding your messages — fix the problem (usually: Postgres is not
running) and re-run.

Switching **direct → durable** needs nothing special. Run
`./install.sh --replay-fallback` afterwards if a fallback file has accumulated.

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

### Running the drainer on Linux

The installer schedules the drainer with launchd, which is macOS-only. On Linux,
install it as a user timer instead:

```ini
# ~/.config/systemd/user/claude-chats-drain.service
[Unit]
Description=Drain the claude-chats outbox into PostgreSQL

[Service]
Type=oneshot
EnvironmentFile=%h/.claude-chats/config.env
ExecStart=/usr/bin/env uv run --project /path/to/claude-chats/localhost/hook drain-outbox
```

```ini
# ~/.config/systemd/user/claude-chats-drain.timer
[Unit]
Description=Drain the claude-chats outbox every 2 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now claude-chats-drain.timer
```

## Troubleshooting

**Nothing is being recorded at all.**
Check the hook is registered: `grep -c record-conversation ~/.claude/settings.json`
should be 2 (one `Stop`, one `UserPromptSubmit`). Hooks are read when Claude Code
starts, so restart it after an install.

**Messages are in the outbox but never reach Postgres.**
Read `/tmp/claude-chats-drain.log`. Then run `./install.sh --drain` by hand — it
prints the real error. Check the agent is loaded with
`launchctl list | grep claude-chats`. The most common cause is that Postgres is
not running: `docker compose -f docker-compose.yml up -d`.

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

That removes the hook entries, the MCP registration and the LaunchAgent, and
deliberately leaves your data alone. To remove that too:

```bash
docker compose -f docker-compose.yml down -v   # drops the database volume
rm -rf ~/.claude-chats                          # outbox, images, config, backups
```

## Where things live

| Path | What |
|---|---|
| `~/.claude-chats/outbox.db` | The SQLite outbox (durable mode) |
| `~/.claude-chats/outbox-fallback.ndjson` | Last-resort capture when the outbox itself fails |
| `~/.claude-chats/images/` | Archived images with JSON sidecars |
| `~/.claude-chats/config.env` | What the installer recorded; read by the maintenance commands |
| `~/.claude-chats/backups/` | Default destination for `--backup` |
| `~/.claude/settings.json` | Where the hook is registered |
| `/tmp/claude-chats-drain.log` | Drainer output |
| Docker volume `localhost_pgdata` | The database itself |
