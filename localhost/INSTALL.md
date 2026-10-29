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
| Optional: a model for Claude Facts | Extracts facts for `recall_facts` | your Claude Code login by default; see [Claude Facts](#claude-facts-recall_facts) |

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
   Linux;
9. sets up Claude Facts with the provider you chose (your Claude Code login by
   default) and installs its 15-minute schedule, unless that provider is not
   usable here, in which case facts are left off with a warning;
10. installs the nightly backup: a LaunchAgent on macOS, a systemd user timer on
    Linux, at 03:30 ([backups](#backups)).

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

## Claude Facts (`recall_facts`)

`recall_facts` answers "what is X?" questions from standalone facts rather than
whole conversations: "the workshop lathe is a Myford ML7", each with the date
it was stated and the date it was last confirmed. It searches them by meaning
and by words, and discounts old ones at a rate that depends on the kind of fact.
For the conversation a fact came from, use `get_conversation`.

The `facts`, `fact_events` and `facts_progress` tables come from `../init.sql`,
so every install and upgrade has them. Facts are written by extraction: Claude
Haiku reads each exchange of a conversation (your message and Claude's replies,
not tool output) and writes down what is durably true. The installer sets that
up as its own schedule, a LaunchAgent (`com.nakomis.claude-chats-facts`) on
macOS or a systemd user timer (`claude-chats-facts`) on Linux, which runs
`python -m facts.single_machine` every 15 minutes. It never runs from the hook
or the drain, so capture stays as quick and as offline as it was. One run at a
time: a run that is still going when the next is due makes that one skip. Facts
are embedded with the same provider and model as your messages, so the vector
half of `recall_facts` lines up with your queries.

### Choosing how facts are extracted

Chosen when you install, like the embedding provider, and remembered in
`~/.claude-chats/config.env`:

| `FACTS_PROVIDER` | Needs | Costs |
|---|---|---|
| `claude-code` (default) | Claude Code, logged in to your account (`claude auth status`) | No extra money: it uses your Claude subscription's usage allowance and rate limits (see below) |
| `anthropic` | `ANTHROPIC_API_KEY` | Haiku API pricing, billed to that key |
| `bedrock` | AWS credentials (`AWS_PROFILE`, `AWS_REGION`), and access to Claude Haiku in your Bedrock account | Bedrock's Haiku pricing, billed to your AWS account |
| `off` | | Nothing; `recall_facts` finds nothing |

```bash
./install.sh                                     # claude-code
FACTS_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... ./install.sh
FACTS_PROVIDER=bedrock AWS_PROFILE=me AWS_REGION=eu-west-2 ./install.sh
FACTS_PROVIDER=off ./install.sh
```

If what a provider needs is missing (Claude Code not logged in, no API key, no
AWS credentials), the installer says so, sets facts to `off` and carries on:
capture and search do not depend on them. Fix it and re-run `./install.sh`; it
remembers what you asked for, so a plain re-run turns facts back on. With
`FACTS_PROVIDER=off` given explicitly, it stays off until you choose again.

On Bedrock, Claude Haiku 5.5 is only reachable through an inference profile.
The default, `global.anthropic.claude-haiku-5-5`, works from any region; to keep
requests within one geography, set `FACTS_MODEL=eu.anthropic.claude-haiku-5-5`
(or `us.`) when you install. Your account needs access to the model: a
`403 ... is not available for this account` in the facts log means it does not.
For a model that takes no effort setting (Haiku 4.5, say), add `FACTS_EFFORT=none`.
A model you set belongs to that provider: switching provider drops it.

There is no Ollama option. Extraction is a harder task than embedding, and in a
comparison on 28 Oct 2026 local 7–8B models found 73% of the facts in a
hand-checked reference set, against 100% for Haiku.

### What `claude-code` runs, and what it uses up

With the default provider, each extraction is your own Claude Code run
headlessly, `claude -p`, as you, on your login. It is isolated from your normal
setup: no tools, no MCP servers, no settings files, hooks disabled (so it cannot
trigger claude-chats' own hook, or any of yours), no `CLAUDE.md` (it runs in an
empty temporary directory), and the session is not saved. Its system prompt is
the extraction prompt in [`../facts/prompts.py`](../facts/prompts.py), and the
exchange is passed on standard input. `ANTHROPIC_API_KEY` and the variables
that would send it to Bedrock or elsewhere are removed from its environment, so
it always uses the login. It never uses `--bare`, which ignores the login.

It counts towards your subscription's usage allowance, like any other use of
Claude Code. Measured on 106 exchanges of real conversations (29 Oct 2026): each
exchange is one Haiku call of about 7 seconds, with roughly 4,800 tokens read
from the prompt cache, 4,600 written to it and 1,260 of output. That is about
65% more output than the same prompt through the API, and the cache writes are
not re-read. No rate limiting was seen. Anthropic's terms for automated use of
a subscription login can change: check the current ones before relying on it,
and use the `anthropic` or `bedrock` provider if they do not suit.

### Older history (backfill)

Only conversations started after facts were first installed are read. That
moment is recorded as `FACTS_INSTALLED_AT` (and `FACTS_SINCE`) in `config.env`
and is kept on every re-run, so upgrading never starts reading your whole
history. A conversation that was already going at that moment is left for a
backfill even if you carry on with it, so that when it is read, it is read from
its first message rather than from the middle. Turning facts off and on again
keeps the starting point: conversations from while they were off are read when
they come back on. To extract from older conversations too:

```bash
./install.sh --facts-backfill                    # all of it
./install.sh --facts-backfill --since 2026-06-01 # from a date
./install.sh --facts-backfill-stop               # back to new conversations only
```

A backfill goes at the same pace as everything else: at most
`FACTS_MAX_EXCHANGES` exchanges per 15-minute run, **10 by default**, so 40 an
hour, and new conversations always go first. Progress is kept per conversation
(`facts_progress`), so stopping, a full allowance, a reboot or a re-run loses
nothing; it carries on where it was. A backfill of 1,000 exchanges takes about a
day.

Why 10: at the measured cost per exchange, 40 an hour on the `claude-code`
provider is roughly 50,000 output tokens and 400,000 cached input tokens of
Haiku an hour, and each run takes a little over a minute. Your own exchanges
arrive far more slowly than that, so new conversations keep up even during a
backfill, while a backfill cannot consume a subscription's allowance in an
afternoon. On a Pro plan, or if you share the allowance with heavy interactive
use, halve it; with the `anthropic` or `bedrock` provider, where it is only
money, raise it:

```bash
FACTS_MAX_EXCHANGES=5 ./install.sh
```

Run one extraction now, rather than waiting for the schedule:

```bash
./install.sh --facts
```

On a single machine it reads the one local database. The distributed edition
can point it at a read replica with `CLAUDE_CHATS_READ_DB_URL`; the installer
never sets that here, and with it unset every read goes to the primary
(`read_from: "primary"` in the result). Don't export it in your shell either:
Claude Code passes its own environment on to MCP servers.

To see how many facts you have:

```bash
docker exec claude-chats-db psql -U claude -d claude_chats \
  -c 'SELECT kind, count(*) FROM facts WHERE deleted_at IS NULL GROUP BY kind'
```

## Maintenance

All of it hangs off the same script:

| Command | What it does |
|---|---|
| `./install.sh` | Install, upgrade, or pick up changed settings |
| `./install.sh --drain` | Drain the outbox into Postgres right now |
| `./install.sh --backfill` | Re-embed every message that has no vector, and rebuild the vector index |
| `./install.sh --replay-fallback` | Recover messages from the fallback file, then drain |
| `./install.sh --backup [dir]` | Back up now, exactly as the nightly schedule does (default `~/.claude-chats/backups/`) |
| `./install.sh --restore [dir\|file]` | Restore the newest nightly and every archived month before it, or a named nightly |
| `./install.sh --uninstall` | Remove the hook, the MCP registration and the drain, facts and backup schedules |
| `./install.sh --facts` | Extract facts from new exchanges now |
| `./install.sh --facts-backfill [--since DATE]` | Extract from older conversations too, at the same rate ([backfill](#older-history-backfill)) |
| `./install.sh --facts-backfill-stop` | Back to conversations since install only |

### Backups

Every night at 03:30 a schedule of its own (`com.nakomis.claude-chats-nightly-backup`
on macOS, `claude-chats-nightly-backup` on Linux) runs the repository's
`scripts/backup.sh`, which writes two things under `~/.claude-chats/backups/`:

| | What | Kept |
|---|---|---|
| `archive/messages/` | One file per complete month of messages, written once | For ever: never pruned |
| `nightly/` | Everything else: the schema, every conversation, every Claude Fact, and the messages not yet archived | The newest 14 (`CLAUDE_CHATS_BACKUP_KEEP`) |

It is the distributed edition's backup (`scripts/convmem-archive.sh`), aimed at a
directory instead of S3. Old months stop changing, so they are written once rather
than every night, and each nightly carries only the current month. Each archived
file's name records its row count, a fingerprint of its contents and its size;
the nightly re-checks them against the database, so a month whose messages
changed later (a backfill, a re-embed) is archived again, and one that failed to
archive stays in the nightly. A failed archive makes the nightly bigger, never
leaves a month in neither place.

The embedding vectors are included, so a restore needs no re-embedding.

`--restore` loads the newest nightly, then every archived month before the month
it starts from, checking each against the size and row count in its name, then
rebuilds the vector index. Give it a nightly file to restore an older one. Dumps
made before October 2026 (whole-database files directly in `backups/`, without
vectors) still restore, followed by a backfill of the vectors, which is the slow
part.

Docker has to be running at 03:30 for the backup; macOS runs a missed one when
the Mac wakes, and Linux at the next boot or login (`Persistent=true`). Check on
it in `/tmp/claude-chats-backup.log` or `journalctl --user -u claude-chats-nightly-backup`.

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
- adds the nightly backup schedule, which earlier versions did not have;
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

The re-run also sets up [Claude Facts](#claude-facts-recall_facts) with the
default provider, your Claude Code login (or `off`, with a warning, if Claude
Code is not logged in), for conversations from that moment on. Nothing from
before the upgrade is read unless you ask for a backfill, and later re-runs keep
that starting point.

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

That removes the hook entries, the MCP registration and the drain, facts and
backup schedules, and deliberately leaves your data (facts and backups
included) alone. To remove that too:

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
| `~/.claude-chats/backups/archive/` | Archived months, written once, never pruned |
| `~/.claude-chats/backups/nightly/` | Nightly dumps; the newest 14 are kept |
| `~/Library/LaunchAgents/com.nakomis.claude-chats-nightly-backup.plist` | The backup schedule (macOS) |
| `~/.config/systemd/user/claude-chats-nightly-backup.{service,timer}` | The backup schedule (Linux; log: `journalctl --user -u claude-chats-nightly-backup`) |
| `/tmp/claude-chats-backup.log` | The backup LaunchAgent's output |
| `~/.claude/settings.json` | Where the hook is registered |
| `~/Library/LaunchAgents/com.nakomis.claude-chats-drain.plist` | The drain schedule (macOS) |
| `~/.config/systemd/user/claude-chats-drain.{service,timer}` | The drain schedule (Linux) |
| `/tmp/claude-chats-drain.log` | The LaunchAgent's drain output |
| `~/Library/LaunchAgents/com.nakomis.claude-chats-facts.plist` | The Claude Facts schedule (macOS) |
| `~/.config/systemd/user/claude-chats-facts.{service,timer}` | The Claude Facts schedule (Linux; log: `journalctl --user -u claude-chats-facts`) |
| `/tmp/claude-chats-facts.log` | The facts LaunchAgent's output: one `facts-run {...}` line per run |
| `~/.claude-chats/facts.lock` | Held while a facts run is going |
| `localhost/facts/` | The Python environment facts run in (the code is `../facts`) |
| Docker volume `localhost_pgdata` | The database itself |
