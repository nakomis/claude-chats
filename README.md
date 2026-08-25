# claude-chats

Records Claude Code conversations to PostgreSQL with vector embeddings for semantic search, exposed via an MCP server.

A hook fires on every user message and again at session end, capturing new messages. Each message is embedded so the MCP tools can do similarity search across every conversation you have ever had with Claude — across every machine you have them on.

## Standalone

Looking for the standalone installer README.md? [Click here](localhost/README.md).

That edition runs everything on one machine — no server, no broker, no cloud account — and is the one to use if you just want this on your laptop. Everything below describes the distributed system.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=claude-chats)

## How it works

Several client machines record into one shared database on a server. Capture and delivery are deliberately split, so the only part that has to be reliable is the part that cannot fail.

![Distributed architecture](docs/architecture/distributed-architecture.svg)

*Source: [`docs/architecture/distributed-architecture.drawio`](docs/architecture/distributed-architecture.drawio). The SVG is regenerated on commit by `githooks/pre-commit` — enable it with `git config core.hooksPath githooks`.*

### Capture — on each client

The hook (`hook/hook/record.py`) fires on `Stop` and `UserPromptSubmit`. It reads the session transcript, works out which messages are new, and **appends them to a local SQLite outbox**. That is all it does: no network I/O, no credentials, no daemon, nothing that can be "down".

It used to write straight to Postgres, and it failed silently — when the Docker daemon holding the database was down, the write threw and the message vanished with no signal at all. Pointing it at a database on another host would have made that worse, not better: more failure modes (network, credentials, being away from home), not fewer. So capture and delivery were split, and only capture has to be reliable. If SQLite itself is unavailable, the hook falls back to an append-only NDJSON file rather than swallowing the error.

Deduplication happens locally, because the hook re-reads the whole transcript on every `Stop` and can no longer ask Postgres what it has already seen. `INSERT OR IGNORE` against a `UNIQUE message_uuid` does it — which is why forwarded rows are kept as tombstones rather than deleted.

### Delivery — forwarder → broker → worker

A **Rust forwarder** (`mac/conversation-memory-forwarder` in [`nakomis/home-infra`](https://github.com/nakomis/home-infra)) drains the outbox and posts to an **ActiveMQ** broker over STOMP. It owns everything that can fail; when it does, the outbox simply grows and drains later. An expired credential or a train with no signal becomes a delay rather than a lost message.

A **Node worker** (`server/worker`) consumes the queue, embeds each message via the server's Ollama, and writes to Postgres in a transaction. Embedding therefore happens on the server, not on the laptop — stopping a session never waits on a model.

### Storage and recall

**PostgreSQL + pgvector** holds `conversations` and `messages`, the latter carrying both a `vector(1024)` embedding (ivfflat, cosine) and a generated `tsvector` (GIN). The `host` column records which machine a conversation happened on, so search can span every machine or filter to one.

The **MCP server** (`mcp/`) runs on the client and queries the remote Postgres directly on port 5433. Only the search query is embedded locally, which is why the client still needs an embedding provider configured even though it never embeds a message.

### Images

Images pasted into Claude live in the transcript as base64 and nowhere else that lasts. The hook stages them to a local directory with a JSON sidecar recording every id, and a `flush-images` LaunchAgent moves them to an SMB share every 15 minutes. The same split again: staging is local and cannot fail; the share is over two wireless hops and is allowed to.

### Backups

A container on the server runs `pg_dump` at 01:00 and pipes it straight to S3. The dump **excludes the embedding vectors** — they are incompressible, they dominate the dump, and they are fully regenerable from message content by `backfill-embeddings`. An EventBridge rule fires a Lambda daily at 02:00 to prune the bucket to the 10 most recent backups. Both are deployed by CDK (`server/cdk`).

## Repository layout

| Path | What |
|---|---|
| `hook/` | The capture hook, embedding helper, backfill and image archiving |
| `mcp/` | The MCP server — hybrid search, transcript retrieval, session listing |
| `server/` | Compose stack (Postgres, ActiveMQ, backup), the Node worker, the CDK stack |
| `init.sql` | Source of truth for the Postgres schema |
| `scripts/` | Backup/restore, LaunchAgent setup, embedding evaluation harnesses |
| `localhost/` | The self-contained [single-machine edition](localhost/README.md) |
| `docs/architecture/` | The diagram above, as `.drawio` source and exported SVG |

## Prerequisites

**Server:** Docker, Node 18+, the AWS CLI with credentials, Ollama, and the AWS CDK (installed for you if absent).

**Each client:** Docker is not needed. [uv](https://docs.astral.sh/uv/), [Claude Code](https://claude.com/claude-code), Python 3.11+, and one of Ollama (local), AWS credentials (Bedrock) or an OpenAI API key — used only to embed search queries.

## Installation

On the server:

```bash
AWS_PROFILE=my-profile AWS_REGION=eu-west-2 bash server/install.sh
```

That starts Postgres, ActiveMQ and the backup container, deploys the CDK stack, pulls the embedding model, and installs LaunchAgents for Ollama and the worker.

On each client:

```bash
SERVER_HOST=myserver.local ./install-client.sh
```

That installs the hook and MCP dependencies, registers the MCP server against the remote database, and configures the hooks.

You also need the Rust forwarder from [`nakomis/home-infra`](https://github.com/nakomis/home-infra) on each client — without it the outbox fills and nothing drains it. This repository contains capture but not delivery; if you want a version that works end to end from one checkout, use the [single-machine edition](localhost/README.md).

Re-running either script is safe — all steps are idempotent.

> **Note:** `install-client.sh` still sets `CLAUDE_CHATS_MODE=remote` and `CLAUDE_CHATS_QUEUE_URL` on the hook command. The current `record.py` reads neither — it always writes to the outbox — so those variables are inert, left over from when the hook posted to the queue itself.

## Embedding providers

Configured independently on the server (for message embedding) and on each client (for query embedding). They must agree: a query embedded with one model will not match messages embedded with another.

| Provider | Default model | Notes |
|---|---|---|
| `ollama` | `mxbai-embed-large` | Local; no data leaves the machine. 512-token context — input is truncated to ~800 chars. |
| `bedrock` | `amazon.titan-embed-text-v2:0` | Also supports `cohere.embed-english-v3`. Requires AWS credentials. |
| `openai` | `text-embedding-3-small` | Requires `OPENAI_API_KEY`. Supports up to 8 192 chars. |

Override the model or output dimensions:

```bash
CLAUDE_CHATS_MODEL=mxbai-embed-large \
CLAUDE_CHATS_DIMENSIONS=1024 \
CLAUDE_CHATS_PROVIDER=ollama \
./install-client.sh
```

`init.sql` declares `vector(1024)`, so a provider with a different output width needs `CLAUDE_CHATS_DIMENSIONS` set to something it supports, or a schema change.

## MCP tools

Once installed, three tools are available inside Claude Code:

| Tool | Description |
|---|---|
| `search_memory` | Hybrid semantic + full-text search across all recorded messages, fused with Reciprocal Rank Fusion. Returns the most relevant excerpts with surrounding context. Also accepts `mode` (`hybrid` / `semantic` / `fulltext`). |
| `get_conversation` | Fetch the full transcript for a session by ID. Supports pagination via `start_seq` / `end_seq`. |
| `list_recent_sessions` | List recent sessions, newest first. Optionally filter by project path. |

## Database

PostgreSQL runs in Docker on the server, published on port 5433:

```bash
# Connect (from anywhere on the network)
psql -h myserver.local -p 5433 -U claude claude_chats

# On the server — stop / start the whole stack
docker compose -f server/docker-compose.yml --profile server down
docker compose -f server/docker-compose.yml --profile server up -d
```

(Compose picks up `server/.env` automatically, which is where `install.sh` records `BUCKET_NAME` and `AWS_REGION` for the backup container.)

Data is persisted in a Docker volume and survives container restarts. `init.sql` is the source of truth for the schema; it is vendored into `home-infra` for the deployed copy, and `tests/test_pg_schema.py` pins a checksum of the DDL so the two cannot silently drift.

## Possible future enhancements

### HyDE (Hypothetical Document Embeddings)

Rather than embedding the raw search query, use a local LLM to generate a *hypothetical* conversation excerpt that would answer the query, then embed that instead. Documents and hypothetical documents occupy similar regions of the embedding space, so this can improve recall significantly — especially for short or abstract queries.

`llama3.2:3b` is a good fit for this: small enough to respond in under a second on a laptop, but capable enough to produce a plausible short excerpt.

```bash
ollama pull llama3.2:3b
```

Note: HyDE is most beneficial when using a model with a long context window. With the current `mxbai-embed-large` setup (512-token / ~800 char limit), a generated excerpt would be truncated heavily. Switching to a model like `nomic-embed-text` (8 192 tokens) first would make HyDE considerably more effective.

### Longer-context embedding model

`nomic-embed-text` supports 8 192 tokens and is available locally via Ollama. Switching to it would allow full messages to be embedded rather than truncated, at the cost of re-embedding all existing messages (dimensions change from 1 024 to 768).

```bash
ollama pull nomic-embed-text
```

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=claude-chats)
