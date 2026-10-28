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

Each fact carries `text`, `kind` (`identity` / `config` / `situation` / `event`),
`subject`, `attribute`, and optionally `temporary` and `expected_until`. Bump
`PROMPT_VERSION` in `prompts.py` whenever the prompt changes, because it is stored
with every fact (`facts.extractor`).

## Scoring a change

```sh
ANTHROPIC_API_KEY=... uv run facts_eval/run.py module:facts.extract:make_extractor
```

See [`facts_eval/`](../facts_eval/README.md). Any prompt change should be scored
against the reference set before it ships.
