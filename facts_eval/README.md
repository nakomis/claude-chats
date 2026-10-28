# facts_eval

Scores a fact extractor against a hand-checked reference set, so a prompt or model
change is judged by numbers rather than by eye. Part of Claude Facts (CHATS-21):
durable facts extracted from conversation-memory.

```sh
uv run facts_eval/run.py mem0:anthropic:claude-haiku-5-5   # extract, judge, print the table
uv run facts_eval/run.py --compare                          # every stored run, side by side
uv run facts_eval/run.py --export <session-id> [...]        # a session's exchanges, for writing reference facts
```

Each run prints one row per stored run:

| Column | Meaning |
|---|---|
| `recall` | Share of reference facts captured (full = 1, partial = ½) |
| `full` / `partial` / `missing` | Reference facts by how well they were captured |
| `facts` | Facts the extractor produced |
| `correct` | Accurate, durable and standalone |
| `wrong` | Contradicts the conversation, or claims what it doesn't say |
| `transient` | True only at that moment ("battery at 84%") |
| `advice` | Only records that the assistant suggested something |
| `trivial` | Too vague to use on its own ("Every box is back up") |
| `unparseable` | LLM replies that weren't valid JSON |

## Extractors

| Spec | What runs |
|---|---|
| `mem0:anthropic:<model>` | Mem0 2.x, its LLM calls sent to the Anthropic API (effort low, system prompt cached). Needs `ANTHROPIC_API_KEY`. |
| `mem0:bedrock:<model-id>` | Mem0 2.x, its LLM calls sent to Bedrock's Converse API |
| `mem0:ollama:<model>` | Mem0 2.x on a local Ollama model, its context window raised to 12k so Mem0's ~9k-token prompt is not cut |
| `module:<module>:<factory>` | Our own extractor: `factory(run_dir)` returns an object whose `add(session, index, exchange)` returns `extractors.AddResult` |

Mem0's embedder is local Ollama (`FACTS_EVAL_EMBED_MODEL`, default `mxbai-q8`).

## What the extractor sees

Each session from CM becomes a list of **exchanges**: Martin's message (or
consecutive messages), then Claude's replies. Only `author` `martin` and `claude`
rows are used. Tool output and `unknown` rows are left out: on 28 Oct 2026 they
made small models run away and carried secrets into the prompt. A message identical
to the one before it is dropped, because CM stores some messages twice. Each
message is cut at 3,000 characters.

## The judge

gpt-oss-120b on Bedrock (`FACTS_EVAL_JUDGE` to change it), chosen because it is not
one of the models being compared. It reads the transcript, the reference facts and
the extracted facts, and labels each. AWS credentials come from the environment
(`AWS_PROFILE`, `AWS_REGION`).

## Data: kept out of this repo

Everything the harness reads or writes lives in `$FACTS_EVAL_DIR` (default
`ignored/facts-eval/`, which git ignores):

- `reference.json` — the reference set. Its facts name hosts, addresses, MACs and
  paths, so it is private.
- `runs/<timestamp>-<extractor>/` — per run: `rows.json` (facts and token counts
  per exchange), `raw.jsonl` (every prompt and raw reply), `verdicts.json`,
  `summary.json`.

Transcripts are read from CM at run time and never stored in the repo.

The reference format, with a made-up example in [`example/`](example/):

```json
{"sessions": [{"session_id": "<CM session id>", "name": "<label>",
               "facts": {"X1": "One standalone, durable fact.", "X2": "..."}}]}
```

Fact IDs must be unique across the whole file. A good reference fact is durable,
makes sense on its own (names the machine or project), keeps the concrete detail
(paths, versions, numbers), and states the corrected version when the conversation
corrected itself.

## Tests

`uv run --with pytest --with mem0ai python -m pytest tests/test_facts_eval.py`
covers exchange building, de-duplication, extractor specs, reply parsing and
scoring, with no database or network.
