#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3", "numpy", "psycopg2-binary"]
# ///
"""
Titan v2 at 256 vs 1024 dimensions, alongside the live mxbai-q8 vectors.

The original bake-off (embed-bakeoff.py + intrinsic-eval.py) only ran Titan at
1024 dims, on a 29.5k-message snapshot, with a random probe set per model. This
reruns the intrinsic metrics on the current corpus with:

  * titan-1024  — amazon.titan-embed-text-v2:0, dimensions=1024
  * titan-256   — amazon.titan-embed-text-v2:0, dimensions=256
  * mxbai-q8    — the vectors already in messages.embedding (production:
                  mxbai-embed-large Q8_0 on Cal, content truncated to 800 chars).
                  Only rows from 2026-05-09 on — older rows hold Titan-1024
                  vectors left over from the original bake-off, and are skipped.

Every model is scored on the *same* probes and pairs, with exact (numpy) kNN
rather than pgvector, so the numbers are directly comparable with each other —
but not with intrinsic_results.json, whose corpus was a third of the size.

Read-only against the live DB. Titan vectors are cached under --cache so an
interrupted embed run resumes where it left off.

  uv run --python 3.11 --script scripts/titan-dims-eval.py embed    # ~18M tokens/size
  uv run --python 3.11 --script scripts/titan-dims-eval.py eval
  uv run --python 3.11 --script scripts/titan-dims-eval.py queries

(--python 3.11 because the repo's .python-version pins 3.7, which uv honours
over the script's requires-python.)
"""

import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import numpy as np
import psycopg2
from botocore.config import Config

DB_URL = "postgresql://claude:claude@luke.local:5432/claude_chats"
TITAN = "amazon.titan-embed-text-v2:0"
SIZES = [256, 1024]
K = 10            # recall@k
N_PROBE = 4000    # probes for recall@k (shared across models)
N_PAIRS = 5000    # pairs for intra/inter similarity (shared across models)
SEED = 151
CHUNK = 2000      # messages per cache file

_CTRL = re.compile(r"[^\t\n\r\x20-\U0010ffff]")


def clean(content: str) -> str:
    # Same normalisation as embed-bakeoff.py.
    return _CTRL.sub("", content).strip()[:8192]


def fetch_messages(with_embeddings: bool):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(name="msgs")  # server-side: don't pull 1GB at once
    cur.itersize = 2000
    cols = "id::text, conversation_id::text, sequence_num, content"
    if with_embeddings:
        cols += ", embedding::text"
    cur.execute(f"SELECT {cols} FROM messages WHERE embedding IS NOT NULL ORDER BY id")
    yield from cur
    conn.close()


# ── Embed ─────────────────────────────────────────────────────────────────────

def cached_ids(cache: Path, dims: int) -> set[str]:
    done = set()
    for f in sorted((cache / f"titan-{dims}").glob("*.npz")):
        done.update(np.load(f)["ids"].tolist())
    return done


def embed(args):
    client = boto3.client(
        "bedrock-runtime",
        region_name=args.region,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"},
                      max_pool_connections=args.workers),
    )

    def one(item):
        msg_id, text, dims = item
        body = json.dumps({"inputText": text, "dimensions": dims, "normalize": True})
        try:
            resp = client.invoke_model(modelId=TITAN, body=body,
                                       contentType="application/json",
                                       accept="application/json")
        except client.exceptions.ValidationException:
            return msg_id, None
        return msg_id, json.loads(resp["body"].read())["embedding"]

    rows = [(i, clean(c)) for i, _conv, _seq, c in fetch_messages(False)]
    rows = [(i, t) for i, t in rows if t]
    print(f"{len(rows)} non-empty messages", flush=True)

    for dims in SIZES:
        out = args.cache / f"titan-{dims}"
        out.mkdir(parents=True, exist_ok=True)
        done = cached_ids(args.cache, dims)
        todo = [(i, t, dims) for i, t in rows if i not in done]
        print(f"\n== titan-{dims}: {len(done)} cached, {len(todo)} to embed", flush=True)

        start, skipped = time.time(), 0
        with ThreadPoolExecutor(args.workers) as pool:
            for n in range(0, len(todo), CHUNK):
                batch = list(pool.map(one, todo[n:n + CHUNK]))
                ok = [(i, v) for i, v in batch if v is not None]
                skipped += len(batch) - len(ok)
                np.savez(out / f"{time.time_ns()}.npz",
                         ids=np.array([i for i, _ in ok]),
                         vecs=np.array([v for _, v in ok], dtype=np.float32))
                done_now = n + len(batch)
                rate = done_now / (time.time() - start)
                eta = (len(todo) - done_now) / rate / 60
                print(f"  titan-{dims} {done_now}/{len(todo)}  {rate:.1f} msg/s  "
                      f"ETA {eta:.1f} min  skipped {skipped}", flush=True)


# ── Eval ──────────────────────────────────────────────────────────────────────

def load_titan(cache: Path, dims: int) -> dict[str, np.ndarray]:
    vecs = {}
    for f in sorted((cache / f"titan-{dims}").glob("*.npz")):
        d = np.load(f)
        vecs.update(zip(d["ids"].tolist(), d["vecs"]))
    return vecs


def normalise(m: np.ndarray) -> np.ndarray:
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def evaluate(args):
    """Intrinsic metrics on the messages whose stored vector really is mxbai-q8.

    Rows up to 2026-05-08 22:59 carry Titan-1024 vectors left over from the
    original bake-off, not mxbai — they're detected (cosine > 0.99 with our
    fresh Titan-1024 vector) and excluded so the mxbai column is genuine.
    """
    print("Loading messages and live vectors…", flush=True)
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT id::text, author, length(content) FROM messages")
    meta = {i: (a, n) for i, a, n in cur}
    conn.close()

    titan = {d: load_titan(args.cache, d) for d in SIZES}
    ids, conv, seq, live, titan_space = [], [], [], [], 0
    for i, c, s, _content, emb in fetch_messages(True):
        if not all(i in titan[d] for d in SIZES):
            continue
        v = np.fromstring(emb[1:-1], sep=",", dtype=np.float32)
        t = titan[1024][i]
        if v @ t / np.linalg.norm(v) / np.linalg.norm(t) > 0.99:
            titan_space += 1
            continue
        ids.append(i); conv.append(c); seq.append(s); live.append(v)
    print(f"Skipped {titan_space} rows whose stored vector is Titan-1024; "
          f"scoring {len(ids)} genuine mxbai-q8 rows", flush=True)

    conv = np.array(conv)
    models = {"mxbai-q8 (live)": normalise(np.stack(live))}
    for d in SIZES:
        models[f"titan-v2-{d}"] = normalise(np.stack([titan[d][i] for i in ids]))

    # Shared probes and pairs — identical for every model.
    rng = random.Random(SEED)
    _, inverse, counts = np.unique(conv, return_inverse=True, return_counts=True)
    eligible = np.flatnonzero(counts[inverse] > K)
    probes = np.array(rng.sample(eligible.tolist(), min(N_PROBE, len(eligible))))
    pos = {(c, s): n for n, (c, s) in enumerate(zip(conv, seq))}
    consecutive = [(n, pos[(c, s + 1)]) for n, (c, s) in enumerate(zip(conv, seq))
                   if (c, s + 1) in pos]
    intra_pairs = np.array(rng.sample(consecutive, min(N_PAIRS, len(consecutive))))
    inter_pairs = []
    while len(inter_pairs) < N_PAIRS:
        a, b = rng.randrange(len(ids)), rng.randrange(len(ids))
        if conv[a] != conv[b]:
            inter_pairs.append((a, b))
    inter_pairs = np.array(inter_pairs)

    hits, tops, results = {}, {}, {"corpus": len(ids), "n_probes": len(probes)}
    for name, m in models.items():
        h, t = [], []
        for b in range(0, len(probes), 250):
            p = probes[b:b + 250]
            sims = m[p] @ m.T
            sims[np.arange(len(p)), p] = -np.inf  # exclude self
            top = np.argpartition(-sims, K, axis=1)[:, :K]
            t.extend(top)
            h.extend((conv[top] == conv[p][:, None]).mean(axis=1))
        hits[name], tops[name] = np.array(h), t
        intra = float(np.mean(np.sum(m[intra_pairs[:, 0]] * m[intra_pairs[:, 1]], axis=1)))
        inter = float(np.mean(np.sum(m[inter_pairs[:, 0]] * m[inter_pairs[:, 1]], axis=1)))
        results[name] = {
            "dims": m.shape[1],
            f"recall@{K}": round(float(hits[name].mean()), 4),
            "intra_sim": round(intra, 4),
            "inter_sim": round(inter, 4),
            "separation": round(intra / inter, 4),
        }

    names = list(models)
    results["paired_diff_95ci"] = {}
    for a, b in [(names[2], names[1]), (names[2], names[0]), (names[1], names[0])]:
        d = hits[a] - hits[b]
        results["paired_diff_95ci"][f"{a} - {b}"] = [
            round(float(d.mean()), 4), round(float(1.96 * d.std() / np.sqrt(len(d))), 4)]
    results[f"top{K}_overlap_256_vs_1024"] = round(float(np.mean(
        [len(set(x) & set(y)) / K for x, y in zip(tops[names[1]], tops[names[2]])])), 4)

    author = np.array([meta[i][0] for i in ids])[probes]
    length = np.array([meta[i][1] for i in ids])[probes]
    groups = [(f"author={a}", author == a) for a in np.unique(author)]
    groups += [("len<=800", length <= 800), ("len>800", length > 800)]
    results["by_group"] = {
        g: {"n": int(mask.sum()), **{n: round(float(hits[n][mask].mean()), 3) for n in names}}
        for g, mask in groups
    }

    print(json.dumps(results, indent=2))
    out = Path(__file__).with_name("titan_dims_results.json")
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Saved {out}")


def queries(args):
    """Titan sizes against the hand-labelled query set (query_set.json).

    mxbai can't be scored here: every labelled message predates 2026-05-09, so
    its stored vector is Titan-1024, not mxbai.
    """
    import math
    client = boto3.client("bedrock-runtime", region_name=args.region)
    qs = json.loads(Path(__file__).with_name("query_set.json").read_text())
    results = {}
    for d in SIZES:
        vecs = load_titan(args.cache, d)
        ids = np.array(list(vecs))
        m = normalise(np.stack([vecs[i] for i in ids]))
        rec, ndcg, mrr, per = [], [], [], []
        for q in qs:
            rel = set(q["relevant_ids"])
            body = json.dumps({"inputText": q["query"], "dimensions": d, "normalize": True})
            v = np.array(json.loads(client.invoke_model(modelId=TITAN, body=body)["body"].read())
                         ["embedding"], dtype=np.float32)
            hit = [i in rel for i in ids[np.argsort(-(m @ v))[:K]]]
            ideal = min(len(rel), K)
            rec.append(sum(hit) / ideal)
            ndcg.append(sum(1 / math.log2(r + 2) for r, h in enumerate(hit) if h)
                        / sum(1 / math.log2(r + 2) for r in range(ideal)))
            mrr.append(next((1 / (r + 1) for r, h in enumerate(hit) if h), 0.0))
            per.append(sum(hit))
        results[f"titan-v2-{d}"] = {
            f"recall@{K}": round(float(np.mean(rec)), 4),
            f"ndcg@{K}": round(float(np.mean(ndcg)), 4),
            "mrr": round(float(np.mean(mrr)), 4),
            "zero_hit_queries": per.count(0),
            "hits_per_query": per,
        }
    print(json.dumps(results, indent=2))
    out = Path(__file__).with_name("titan_dims_query_results.json")
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"Saved {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cmd", choices=["embed", "eval", "queries"])
    parser.add_argument("--cache", type=Path, default=Path("/tmp/titan-dims-cache"))
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    {"embed": embed, "eval": evaluate, "queries": queries}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
