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
| `claude_code.py` | `ClaudeCodeExtractor`: the same contract, through `claude -p` on the user's own Claude Code login |
| `service.py` | The extractor service: new exchanges to facts, then embeddings (see below) |

Each fact carries `text`, `kind` (`identity` / `config` / `situation` / `event`),
`subject`, `attribute`, and optionally `temporary` and `expected_until`. Bump
`PROMPT_VERSION` in `prompts.py` whenever the prompt changes, because it is stored
with every fact (`facts.extractor`).

## Providers

`FACTS_PROVIDER` chooses how Haiku is reached, for the service and for
`module:facts.extract:make_extractor` in the eval harness:

- `anthropic` (the default; the distributed service on Mace): the Anthropic API,
  with `ANTHROPIC_API_KEY`.
- `claude-code` (CHATS-39; the single-machine edition): `claude -p` on the user's
  own Claude Code login, so no API key is needed and calls count against their
  subscription. Each call is an isolated headless run: `--tools ""`,
  `--strict-mcp-config`, `--no-session-persistence`, `--setting-sources ""`, hooks
  disabled, the system prompt from a file and the exchange on stdin, in an empty
  temporary directory so no `CLAUDE.md` is found. `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` are removed from the child's environment, otherwise Claude
  Code would bill the key instead of the login, and so are the variables that would
  route it to Bedrock, Vertex or Foundry, another base URL or another model
  (`STRIPPED_ENV` in `claude_code.py`). Never `--bare`: it ignores the login.
  The reply is held to the prompt's output shape with `--json-schema`; the facts
  still go through the same parsing and credential filter. A missing `claude`, a
  CLI that rejects the options, a login that has lapsed, a usage limit, an API
  error (status 401/402/403/404/408/429/5xx, or a 400 about billing or permissions,
  or an error with no status), or a call that outlives its timeout (`FACTS_API_TIMEOUT_SECONDS` in the service,
  `FACTS_CLAUDE_TIMEOUT_SECONDS` in the harness, 120 s; the child is killed) are
  systemic, so the service's circuit breaker treats them as an outage. Other
  settings: `FACTS_CLAUDE_BIN` (default `claude` on `PATH`) and, in the harness,
  `FACTS_CLAUDE_CODE_WORKERS` (sessions in parallel, default 3: each call is a
  whole process).

The facts are tagged with the same `facts.extractor` value either way
(`<model>+<PROMPT_VERSION>`): same model, same prompt.

## The service

`python -m facts.service [--limit N] [--dry-run]` is one run of the extractor
(CHATS-25); a systemd timer on Mace fires it every 15 minutes (the image and
units live in home-infra, `mace/deployment/facts/`). Each run:

1. finds conversations with messages past their `facts_progress.last_seq` and
   splits them into exchanges with `facts_eval.export.split_exchanges` (the same
   rules the eval harness scores with). An exchange is processed once Martin has
   sent a later message or the session has been idle for 30 minutes;
2. calls `Extractor.extract` per exchange, in order, and inserts the facts and
   advances `facts_progress` in one transaction. An exchange that fails on its
   content (a reply that is still not valid JSON after the repair attempt, a
   refusal, a database constraint error) is retried on later runs, and skipped
   (logged, counted in `gave_up`) after 3 attempts. Failures that say nothing
   about the exchange (auth, billing, rate limits, 5xx, network, a lost database
   connection) are **not** attempts: after 3 in a row the run stops, and the next
   tick tries again, so an outage cannot make the service give up on anything;
3. embeds facts with no vector through Ollama (`mxbai-q8`, 800 characters,
   `truncate: true`, as Cal's embedding-consumer does) and tags `facts.embedder`;
4. prints a `facts-run {...}` JSON line: exchanges, facts, errors, tokens. The
   exit status is non-zero when the run had errors and produced nothing (so a
   systemd oneshot unit fails and the failed-unit alert fires).

A conversation whose tail cannot yet form an exchange (a last message of Martin's
with no reply) is looked at again on each run. That is harmless: one indexed read
and no model call, and progress is deliberately not moved past it, so a late reply
is not lost.

`--dry-run` calls the model but writes and embeds nothing, and prints the facts.
Nothing the model was sent or replied is stored or logged.

Configuration is environment only: `FACTS_DATABASE_URL` (or `CM_DATABASE_URL`),
`ANTHROPIC_API_KEY` (else fetched from SSM at `FACTS_ANTHROPIC_KEY_PARAM`,
default `/conversation-memory/prod/facts/anthropic-api-key`), `FACTS_EMBED_URL`
(or `OLLAMA_BASE_URL`), and the optional `FACTS_EMBED_MODEL`, `FACTS_EMBED_TAG`,
`FACTS_MAX_EXCHANGES` (40), `FACTS_IDLE_MINUTES` (30), `FACTS_MAX_ATTEMPTS` (3),
`FACTS_MAX_RUNTIME_SECONDS` (780), `FACTS_EMBED_BATCH` (200),
`FACTS_MAX_SYSTEMIC_ERRORS` (3), `FACTS_API_TIMEOUT_SECONDS` (120),
`FACTS_STATEMENT_TIMEOUT_MS` (60000), `FACTS_MODEL` and `FACTS_PROVIDER` (above;
with `claude-code` no API key is used).

**`FACTS_SINCE` is required for a real run.** It is an ISO date or time; messages
before it are ignored. An empty or missing value is an error (the run refuses
before doing anything), because the alternative would be paid calls for the whole
history. To process everything on purpose, set `FACTS_SINCE=all`. The effective
value is logged at the start of every run. A `--dry-run` may omit it (it looks at
everything, bounded by `--limit`).

Tests: `tests/test_facts_service.py`. The database tests need a Postgres with
pgvector where the role may CREATE DATABASE: set `FACTS_TEST_ADMIN_DSN` and each
run makes (and drops) a uniquely named scratch database.

## Scoring a change

```sh
ANTHROPIC_API_KEY=... uv run facts_eval/run.py module:facts.extract:make_extractor
uv run facts_eval/run.py module:facts.extract:make_claude_code_extractor   # on your Claude Code login
```

See [`facts_eval/`](../facts_eval/README.md). Any prompt change should be scored
against the reference set before it ships.
