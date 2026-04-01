# claude-chats

Records Claude Code conversations to PostgreSQL with vector embeddings for semantic search, exposed via an MCP server.

A hook fires on every user message and again at session end, saving new messages to Postgres incrementally. Each message is embedded using your chosen provider so the MCP tools can do similarity search across all past conversations.

## How it works

```
User sends a message / session ends
        │
        ▼
  Hook fires (UserPromptSubmit + Stop)
  (hook/record.py)
        │
        ├─ reads ~/.claude/projects/.../conversations/*.jsonl
        ├─ inserts new messages into PostgreSQL
        └─ generates + stores vector embedding per message
                        │
                        ▼
              PostgreSQL + pgvector
                        │
                        ▼
           MCP server (mcp/server.py)
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
       search_memory        get_conversation
    (semantic search)     (fetch full transcript)
```

## Prerequisites

- Docker (for PostgreSQL + pgvector)
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.ai/code)
- Python 3.11+
- One of: Ollama (local), AWS credentials (Bedrock), or an OpenAI API key

## Installation

```bash
# Ollama (default — no data leaves localhost)
CLAUDE_CHATS_PROVIDER=ollama ./install.sh

# Amazon Bedrock
CLAUDE_CHATS_PROVIDER=bedrock ./install.sh

# OpenAI
CLAUDE_CHATS_PROVIDER=openai OPENAI_API_KEY=sk-... ./install.sh
```

Re-running is safe — all steps are idempotent. To switch provider, re-run with the new `CLAUDE_CHATS_PROVIDER`.

## Embedding providers

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
./install.sh
```

## MCP tools

Once installed, three tools are available inside Claude Code:

| Tool | Description |
|---|---|
| `search_memory` | Semantic search across all recorded messages. Accepts a natural-language query and returns the most relevant excerpts with surrounding context. |
| `get_conversation` | Fetch the full transcript for a session by ID. Supports pagination via `start_seq` / `end_seq`. |
| `list_recent_sessions` | List recent sessions, newest first. Optionally filter by project path. |

## Database

PostgreSQL runs in Docker on port 5433:

```bash
# Connect
psql -h localhost -p 5433 -U claude claude_chats

# Stop
docker compose down

# Start
docker compose up -d
```

Data is persisted in a Docker volume (`claude_chats_data`) and survives container restarts.

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
