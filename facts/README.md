# facts

Claude Facts (CHATS-21): durable facts extracted from conversation-memory, one
Claude Haiku call per exchange.

**Licence: Apache-2.0, not CC0.** This folder is the exception to the rest of the
repository. `prompts.py` is adapted from Mem0's `ADDITIVE_EXTRACTION_PROMPT` and
`generate_additive_extraction_prompt` ([mem0ai/mem0](https://github.com/mem0ai/mem0),
`mem0/configs/prompts.py` at commit `26cdefb4`, Copyright 2023 Taranjeet Singh),
modified as described at the top of that file. Mem0's licence is in
[`LICENSE`](LICENSE); Mem0 ships no NOTICE file. Everything in this folder is under
the same licence so the boundary stays simple.

## What's here

| File | What |
|---|---|
| `prompts.py` | The system prompt (fixed, cached) and the per-exchange user prompt |
| `extract.py` | `Extractor.extract(...)`: one exchange in, a list of `Fact`s out; plus the adapter `facts_eval` uses |
| `service.py` | The extractor service: new exchanges to facts, then embeddings (see below) |

Each fact carries `text`, `kind` (`identity` / `config` / `situation` / `event`),
`subject`, `attribute`, and optionally `temporary` and `expected_until`. Bump
`PROMPT_VERSION` in `prompts.py` whenever the prompt changes, because it is stored
with every fact (`facts.extractor`).

## The service

`python -m facts.service [--limit N] [--dry-run]` is one run of the extractor
(CHATS-25); a systemd timer on Mace fires it every 15 minutes (the image and
units live in home-infra, `mace/deployment/facts/`). Each run:

1. finds conversations with messages past their `facts_progress.last_seq` and
   splits them into exchanges with `facts_eval.export.split_exchanges` (the same
   rules the eval harness scores with). An exchange is processed once Martin has
   sent a later message or the session has been idle for 30 minutes;
2. calls `Extractor.extract` per exchange, in order, and inserts the facts and
   advances `facts_progress` in one transaction. A failed exchange is retried on
   later runs, and skipped (logged, counted in `gave_up`) after 3 attempts;
3. embeds facts with no vector through Ollama (`mxbai-q8`, 800 characters,
   `truncate: true`, as Cal's embedding-consumer does) and tags `facts.embedder`;
4. prints a `facts-run {...}` JSON line: exchanges, facts, errors, tokens.

`--dry-run` calls the model but writes and embeds nothing, and prints the facts.
Nothing the model was sent or replied is stored or logged.

Configuration is environment only: `FACTS_DATABASE_URL` (or `CM_DATABASE_URL`),
`ANTHROPIC_API_KEY` (else fetched from SSM at `FACTS_ANTHROPIC_KEY_PARAM`,
default `/conversation-memory/prod/facts/anthropic-api-key`), `FACTS_EMBED_URL`
(or `OLLAMA_BASE_URL`), and the optional `FACTS_EMBED_MODEL`, `FACTS_EMBED_TAG`,
`FACTS_MAX_EXCHANGES` (40), `FACTS_IDLE_MINUTES` (30), `FACTS_MAX_ATTEMPTS` (3),
`FACTS_MAX_RUNTIME_SECONDS` (780), `FACTS_EMBED_BATCH` (200), `FACTS_SINCE` (ignore
messages before this date; there is no backfill of history unless it is empty)
and `FACTS_MODEL`.

Tests: `tests/test_facts_service.py`. The database tests need a Postgres with
pgvector where the role may CREATE DATABASE: set `FACTS_TEST_ADMIN_DSN` and each
run makes (and drops) a uniquely named scratch database.

## Scoring a change

```sh
ANTHROPIC_API_KEY=... uv run facts_eval/run.py module:facts.extract:make_extractor
```

See [`facts_eval/`](../facts_eval/README.md). Any prompt change should be scored
against the reference set before it ships.
