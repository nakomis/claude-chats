# Titan Text Embeddings v2 at 256 vs 1024 dimensions — evaluation

*1 October 2026. Written for a Claude (or engineer) picking this up cold; self-contained.*

## TL;DR

- **Titan v2 at 256 dims is measurably but modestly worse than at 1024**: −2.2 points recall@10 (≈5% relative, 95% CI ±0.3), 70% top-10 neighbour overlap with 1024. On a hand-labelled query set: recall@10 0.282 vs 0.298, nDCG@10 0.244 vs 0.267.
- **Titan-256 still edges out the model currently in production** (mxbai-embed-large, Q8_0, 1024 dims): +0.9 points (CI ±0.6). **Titan-1024 beats it by +3.1** (CI ±0.6) and wins in every slice.
- **Incidental production bug found**: the live table mixes two embedding spaces. The 29,499 oldest messages (up to 2026-05-08 22:59 UTC) hold Titan-1024 vectors left over from the original model bake-off; everything since holds mxbai-q8 vectors. Queries are embedded with mxbai-q8, so **semantic search cannot reach anything before 9 May 2026** (≈29% of the corpus). Only the full-text half of hybrid search finds those rows.
- **Recommendation**: migrate the whole corpus to Titan v2 at 1024. It is the best on every measure, fixes the mixed-space bug in one go, and every message has already been embedded at both sizes during this test. Choose 256 only if storage or index size matters; at ~100k rows it doesn't (≈400 MB vs ≈100 MB of raw vectors).

## Background

**The system.** `claude-chats` ("conversation-memory") records every Claude Code conversation into Postgres + pgvector so later Claude sessions can search past work via an MCP server. A Claude Code hook captures each message; it's queued (SQS) and an embedding consumer on a low-power home server embeds it and inserts it into `messages.embedding vector(1024)`, with an ivfflat cosine index. Search is hybrid: vector similarity plus Postgres full-text (`tsvector`).

**The corpus.** About 100,800 messages across 293 conversations (March–October 2026): user prompts, Claude's replies, and tool output, from software, infrastructure and home-lab work. No message content is reproduced in this document.

**Current production model.** `mxbai-embed-large` via Ollama, quantised to Q8_0 (`mxbai-q8`), 1024 dims, input truncated to 800 characters. Chosen because the home server has no AVX2/F16C: full-precision mxbai took 22–31 s per embedding there, Q8_0 takes ~1.7 s, and Q8_0 vectors match full-precision ones at cosine 0.9996, so no re-embed was needed. It keeps all inference local.

**The original bake-off (HOME-151, spring 2026).** Eight models were run over the then-corpus of 29.5k messages, each at its native width. Titan v2 (1024) ranked first: recall@10 0.542 vs mxbai's 0.486, and similarity separation 2.59 vs 1.22. Titan was never tested at any other dimension. Results: `scripts/intrinsic_results.json`.

**The question.** Titan v2 supports 256, 512 and 1024 output dims (the dimension is a request parameter; 256 is not just truncation of 1024 on our side). How much quality does 256 give up, and how does it compare with what's actually in production?

## Method

Script: `scripts/titan-dims-eval.py` (`embed`, `eval`, `queries` subcommands). Results: `scripts/titan_dims_results.json`, `scripts/titan_dims_query_results.json`.

1. **Embed.** Every message (100,849 non-empty) was embedded with `amazon.titan-embed-text-v2:0` at `dimensions=256` and at `dimensions=1024`, `normalize=true`, input = control-character-stripped content truncated to 8,192 chars (same normalisation as the original bake-off). Bedrock us-east-1, 16 threads, ~90–100 msg/s, ~20 min per size, zero rejections. ~18M input tokens per size, ≈ $0.36 per size at $0.02/M tokens. Vectors cached locally as `.npz`; the live DB was only read.
2. **Detect the mixed space.** Comparing each stored vector with our fresh Titan-1024 vector of the same message gives cosine 1.000 for exactly the first 29,499 rows by `created_at` (to 2026-05-08 22:59:32) and ≈0 for every row after; a fresh mxbai-q8 embedding matches the later rows at 1.000 and the earlier ones at ≈0. No interleaving. These earlier rows were excluded from the three-way comparison so the "mxbai" column is genuinely mxbai. (Re-embedding them with mxbai wasn't practical: ~40 h on the available Intel Mac, ~14 h on the production consumer.)
3. **Intrinsic evaluation**, same as the original bake-off but tightened. The ground truth is conversation membership: a message's nearest neighbours *should* mostly come from the same conversation. It runs on the 71,350 post-May messages (164 conversations).
   - **recall@10**: for each probe message, the fraction of its 10 nearest neighbours (exact cosine kNN in numpy, self excluded) that share its conversation. 4,000 probes, drawn from conversations with more than 10 messages.
   - **intra/inter similarity**: mean cosine between 5,000 consecutive-message pairs vs 5,000 random cross-conversation pairs. Separation = intra/inter.
   - **Identical probes and pairs for every model**, fixed seed, so differences are paired. 95% CIs come from the per-probe paired differences.
4. **Labelled query evaluation.** `scripts/query_set.json`: 20 natural-language queries with 123 hand-marked relevant message IDs (2–8 each). recall@10, nDCG@10 and MRR over the full 100,849-message Titan corpora. **Titan only.** Every labelled message predates 9 May, so its stored vector is Titan, not mxbai, and mxbai can't be scored on this set without re-embedding.

## Results

### Intrinsic: 71,350 post-May messages, 4,000 shared probes

| Model | Dims | recall@10 | intra sim | inter sim | separation |
|---|---|---|---|---|---|
| **Titan v2** | 1024 | **0.458** | 0.248 | 0.111 | **2.24** |
| Titan v2 | 256 | 0.436 | 0.347 | 0.225 | 1.54 |
| mxbai-q8 (production) | 1024 | 0.427 | 0.595 | 0.511 | 1.16 |

Paired differences in recall@10 (95% CI):

| Comparison | Δ |
|---|---|
| Titan-1024 − Titan-256 | **+0.0217 ± 0.0033** |
| Titan-1024 − mxbai-q8 | **+0.0305 ± 0.0064** |
| Titan-256 − mxbai-q8 | +0.0088 ± 0.0063 |

Top-10 neighbour overlap between Titan-256 and Titan-1024: **70.3%**. Roughly 3 of every 10 results change when you drop to 256.

Recall@10 by slice of probe message:

| Slice | n | mxbai-q8 | Titan-256 | Titan-1024 |
|---|---|---|---|---|
| author = Claude | 353 | 0.365 | 0.415 | **0.442** |
| author = user | 176 | 0.359 | 0.378 | **0.406** |
| author = tool output | 622 | 0.332 | 0.343 | **0.361** |
| author unrecorded (pre-attribution rows) | 2,849 | 0.460 | 0.462 | **0.484** |
| ≤ 800 chars | 3,115 | 0.419 | 0.419 | **0.443** |
| > 800 chars | 885 | 0.454 | 0.494 | **0.508** |

Titan-1024 leads in every slice. Titan-256's edge over mxbai is concentrated in Claude-authored and long messages. That is consistent with mxbai being fed only the first 800 characters, while Titan sees up to 8,192.

### Labelled queries: 20 queries, full corpus, Titan only

| Model | recall@10 | nDCG@10 | MRR | queries with zero hits |
|---|---|---|---|---|
| **Titan v2 1024** | **0.298** | **0.267** | **0.398** | 5 |
| Titan v2 256 | 0.282 | 0.244 | 0.367 | 5 |

The same 5 queries miss at both sizes; per-query hit counts differ by at most one. With 20 queries this is directionally consistent with the intrinsic result, not independently significant.

## Interpretation and caveats

- **The 256 penalty is real but small**, about 5% relative on both evaluations. It's in line with what dimension-reduced embedding models typically lose. Separation and raw cosine values shouldn't be compared across dimensions: lower-dimensional spaces have a higher baseline similarity between unrelated texts. Recall is the like-for-like measure.
- **Absolute numbers are not comparable with the spring bake-off.** The corpus is 3.4× larger, conversations are much longer (~435 messages each on average post-May), and the old probe sets were random per model. The spring figures (Titan 0.542, mxbai 0.486) should not be lined up against these. The rankings agree, though.
- **The intrinsic metric is a proxy.** "Same conversation" rewards topical clustering, and does so more generously the longer conversations get. The labelled set measures real query→message retrieval but is small (20 queries) and was built from mxbai candidate lists, which biases it towards whatever mxbai surfaced.
- **Production truncation is part of what's measured.** mxbai's context is 512 tokens, so production truncates at 800 chars; Titan accepts 8,192 tokens. The comparison is "production as deployed" vs Titan, not a model-only comparison at matched input.
- **Not tested:** Titan at 512 (expected to sit between the two); pgvector `halfvec` (16-bit storage), which halves storage at 1024 with negligible quality loss and is an alternative to dropping dims; binary embeddings.

## The mixed-embedding-space bug

- **Symptom.** Semantic (vector) search never returns messages from before 2026-05-09. Hybrid search masks it partly, because the full-text leg still matches them.
- **Cause (likely, not verified).** The original bake-off ran on a separate database copy and wrote its winning Titan-1024 vectors into `messages.embedding`. The live database was later seeded from that copy, around the 8 May backup/restore work. The live pipeline then carried on embedding new messages with mxbai, and nothing re-embedded the seeded rows. Both models emit 1024 floats, so the `vector(1024)` column accepted both without complaint.
- **Lesson.** Store the embedding model and dimension alongside each vector (e.g. an `embedding_model` column), and have the search path filter on it or refuse a mismatch. Dimension checks alone don't catch a model switch.
- **Fix options.** (a) Re-embed the 29,499 old rows with mxbai-q8: ~14 h on the home server, keeps everything local. (b) Migrate everything to Titan v2: ~40 min of Bedrock time and well under $1, or near zero, since the vectors from this test exist. Queries then need a Bedrock call per search, and message text leaves the house to AWS, which the current design deliberately avoids.

## Recommendation

1. **Fix the mixed space regardless of model choice.** It is silently hiding ~29% of history from semantic search.
2. **If moving to Titan, use 1024, not 256.** 256's only benefit is a 4× smaller vector, and at ~100k rows (≈400 MB at 1024 float32, ≈200 MB as `halfvec`) that buys little. The cost is about 2 points of recall and ~30% of top-10 results changing.
3. **If staying local**, mxbai-q8 is within ~3 points of Titan-1024 on this metric. The privacy and running-cost benefits of local inference may well outweigh that. Fix the old rows and add the model-tag column.

## Reproducing

```bash
# From the claude-chats repo; needs AWS credentials with Bedrock access to Titan v2 in us-east-1
# and read access to the conversation-memory Postgres.
uv run --python 3.11 --script scripts/titan-dims-eval.py embed   --cache /path/to/cache
uv run --python 3.11 --script scripts/titan-dims-eval.py eval    --cache /path/to/cache
uv run --python 3.11 --script scripts/titan-dims-eval.py queries --cache /path/to/cache
```

`--python 3.11` is needed because the repo's `.python-version` pins 3.7, and uv honours it over the script's `requires-python`.
