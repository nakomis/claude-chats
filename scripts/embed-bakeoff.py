#!/usr/bin/env python3
"""
Embedding model bake-off (HOME-151).

Embeds all messages with each candidate model into the message_embeddings
side table, then evaluates retrieval quality against a labelled query set.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import psycopg2
import psycopg2.extras

DB_URL = "postgresql://claude:claude@localhost:5433/claude_chats"
OLLAMA_URL = "http://localhost:11434"
BEDROCK_REGION = "us-east-1"
MODEL_TAG = ""  # set from --tag; appended to model names stored in DB

MODELS = [
    # (name, dims, source, workers)   source: "ollama" | "bedrock" | "bedrock-cohere"
    ("mxbai-embed-large",                1024, "ollama",        1),
    ("nomic-embed-text:v1.5",            768,  "ollama",        1),
    ("bge-m3",                           1024, "ollama",        1),
    ("bge-large",                        1024, "ollama",        1),
    ("snowflake-arctic-embed:l",         1024, "ollama",        1),
    ("amazon.titan-embed-text-v2:0",     1024, "bedrock",      16),
    ("cohere.embed-english-v3",          1024, "bedrock-cohere", 16),
    ("cohere.embed-v4:0",                1536, "bedrock-cohere",  5),
]


# ── Ollama ────────────────────────────────────────────────────────────────────

def embed_ollama(model: str, text: str) -> list[float]:
    payload = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",  # OLLAMA_URL may be overridden via --ollama-url
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["embeddings"][0]


# ── Bedrock ───────────────────────────────────────────────────────────────────

_bedrock_client = None

def _bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock_client


def embed_bedrock(model: str, text: str) -> list[float]:
    body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})
    resp = _bedrock().invoke_model(
        modelId=model,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


# ── Cohere (Bedrock) ──────────────────────────────────────────────────────────

def embed_cohere(model: str, text: str, input_type: str = "search_document") -> list[float]:
    is_v4 = "embed-v4" in model or "embed_v4" in model
    body = json.dumps({
        "texts": [text],
        "input_type": input_type,
        "truncate": "END",
        **({"embedding_types": ["float"]} if is_v4 else {}),
    })
    resp = _bedrock().invoke_model(
        modelId=model,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    data = json.loads(resp["body"].read())
    if is_v4:
        return data["embeddings"]["float"][0]
    return data["embeddings"][0]


# ── Dispatch ──────────────────────────────────────────────────────────────────

def embed(model: str, text: str, source: str = "ollama",
          input_type: str = "search_document") -> list[float]:
    if source == "bedrock":
        return embed_bedrock(model, text)
    if source == "bedrock-cohere":
        return embed_cohere(model, text, input_type)
    return embed_ollama(model, text)


def model_available(model: str, source: str = "ollama") -> bool:
    try:
        embed(model, "test", source)
        return True
    except Exception:
        return False


# ── Embedding run ─────────────────────────────────────────────────────────────

def _embed_one(args):
    """Worker function for parallel embedding."""
    msg_id, content, model, source = args
    text = "".join(ch for ch in content if ch >= " " or ch in "\t\n\r").strip()[:8192]
    if not text:
        return msg_id, None
    try:
        vec = embed(model, text, source, input_type="search_document")
        return msg_id, vec
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return msg_id, "SKIP"
        raise
    except Exception as e:
        err = str(e)
        if "400" in err or "ValidationException" in err:
            return msg_id, "SKIP"
        return msg_id, f"ERROR:{err[:120]}"


def run_embeddings(model: str, dims: int, source: str = "ollama",
                   batch: int = 64, max_workers: int = 1):
    db_model = model + MODEL_TAG  # e.g. "mxbai-embed-large@leia"
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # How many already done for this model?
    cur.execute(
        "SELECT COUNT(*) FROM message_embeddings WHERE model_name = %s",
        (db_model,),
    )
    done = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM messages")
    total = cur.fetchone()[0]

    print(f"\n{'='*60}")
    print(f"Model: {db_model}  dims={dims}  workers={max_workers}")
    print(f"Progress: {done}/{total} already embedded")

    if done == total:
        print("Already complete — skipping.")
        conn.close()
        return

    # Fetch all message ids + content not yet embedded for this model
    cur.execute(
        """
        SELECT m.id, m.content
        FROM messages m
        WHERE NOT EXISTS (
            SELECT 1 FROM message_embeddings e
            WHERE e.message_id = m.id AND e.model_name = %s
        )
        ORDER BY m.id
        """,
        (db_model,),
    )
    rows = cur.fetchall()
    print(f"To embed: {len(rows)}")

    start = time.time()
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_embed_one, args): args[0] for args in
                   ((msg_id, content, model, source) for msg_id, content in rows)}

        insert_buf = []
        for i, future in enumerate(as_completed(futures)):
            msg_id, vec = future.result()
            if vec is None:
                continue
            if vec == "SKIP":
                continue
            if isinstance(vec, str) and vec.startswith("ERROR:"):
                print(f"  ERROR {msg_id}: {vec[6:]}")
                continue

            insert_buf.append((str(msg_id), db_model, str(vec)))
            completed += 1

            if len(insert_buf) >= batch:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO message_embeddings (message_id, model_name, embedding) VALUES %s ON CONFLICT DO NOTHING",
                    insert_buf,
                )
                insert_buf.clear()
                conn.commit()
                elapsed = time.time() - start
                rate = completed / elapsed
                remaining = (len(rows) - i - 1) / rate if rate > 0 else 0
                print(f"  {i+1}/{len(rows)}  {rate:.1f} msg/s  ETA {remaining/60:.1f}min")

        if insert_buf:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO message_embeddings (message_id, model_name, embedding) VALUES %s ON CONFLICT DO NOTHING",
                insert_buf,
            )
            conn.commit()

    conn.commit()
    elapsed = time.time() - start
    rate = len(rows) / elapsed if elapsed > 0 else 0
    print(f"Done: {len(rows)} messages in {elapsed:.1f}s  ({rate:.1f} msg/s)")

    cur.execute(
        """
        INSERT INTO embedding_perf (model_name, msgs_embedded, elapsed_s, msgs_per_s)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (model_name) DO UPDATE
          SET msgs_embedded = EXCLUDED.msgs_embedded,
              elapsed_s     = EXCLUDED.elapsed_s,
              msgs_per_s    = EXCLUDED.msgs_per_s,
              recorded_at   = now()
        """,
        (db_model, len(rows), round(elapsed, 1), round(rate, 1)),
    )
    conn.commit()
    conn.close()


# ── Evaluation ────────────────────────────────────────────────────────────────

def get_model_sizes() -> dict[str, float]:
    """Return {ollama_name: size_gb} for all pulled models."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        sizes = {}
        for m in data.get("models", []):
            gb = m["size"] / 1e9
            sizes[m["name"]] = gb
            sizes[m["name"].split(":")[0]] = gb
        return sizes
    except Exception:
        return {}


def evaluate(query_set_path: str):
    """
    Evaluate each model against a labelled query set.

    Query set JSON format:
    [
      {
        "query": "what did I do about the Taiga postgres migration?",
        "relevant_ids": ["uuid1", "uuid2", ...]
      },
      ...
    ]
    """
    with open(query_set_path) as f:
        queries = json.load(f)

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    results = {}

    for model, dims, source, _workers in MODELS:
        cur.execute(
            "SELECT COUNT(DISTINCT message_id) FROM message_embeddings WHERE model_name = %s",
            (model,),
        )
        count = cur.fetchone()[0]
        if count == 0:
            print(f"Skipping {model} — not embedded yet")
            continue

        ndcg_scores = []
        mrr_scores = []

        for q in queries:
            query_text = q["query"]
            relevant = set(q["relevant_ids"])
            k = 10

            vec = embed(model, query_text, source, input_type="search_query")
            vec_str = str(vec)

            cur.execute(
                """
                SELECT message_id::text
                FROM message_embeddings
                WHERE model_name = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (model, vec_str, k),
            )
            retrieved = [row[0] for row in cur.fetchall()]

            # NDCG@k
            dcg = sum(
                (1 / __import__("math").log2(rank + 2))
                for rank, doc_id in enumerate(retrieved)
                if doc_id in relevant
            )
            ideal_hits = min(len(relevant), k)
            idcg = sum(1 / __import__("math").log2(i + 2) for i in range(ideal_hits))
            ndcg = dcg / idcg if idcg > 0 else 0.0
            ndcg_scores.append(ndcg)

            # MRR
            mrr = 0.0
            for rank, doc_id in enumerate(retrieved):
                if doc_id in relevant:
                    mrr = 1.0 / (rank + 1)
                    break
            mrr_scores.append(mrr)

        avg_ndcg = sum(ndcg_scores) / len(ndcg_scores) if ndcg_scores else 0
        avg_mrr = sum(mrr_scores) / len(mrr_scores) if mrr_scores else 0
        results[model] = {"ndcg@10": avg_ndcg, "mrr": avg_mrr, "n_queries": len(queries)}

    perf_cur = conn.cursor()
    perf_cur.execute("SELECT model_name, msgs_per_s FROM embedding_perf")
    perf = {row[0]: row[1] for row in perf_cur.fetchall()}

    conn.close()

    sizes = get_model_sizes()

    print("\n" + "="*80)
    print("RESULTS")
    print("="*80)
    print(f"{'Model':<35} {'NDCG@10':>8} {'MRR':>8} {'Size GB':>8} {'msg/s':>7} {'Queries':>8}")
    print("-"*80)
    for model, r in sorted(results.items(), key=lambda x: -x[1]["ndcg@10"]):
        size = sizes.get(model, 0)
        size_str = f"{size:.2f}" if size else "  ?"
        rate = perf.get(model)
        rate_str = f"{rate:.1f}" if rate else "  ?"
        print(f"{model:<35} {r['ndcg@10']:>8.4f} {r['mrr']:>8.4f} {size_str:>8} {rate_str:>7} {r['n_queries']:>8}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Embedding bake-off tool")
    parser.add_argument("--ollama-url", default=None,
                        help="Override Ollama base URL (e.g. http://leia.local:11434)")
    parser.add_argument("--tag", default="",
                        help="Suffix appended to model names in DB (e.g. @leia)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("embed", help="Embed all messages with all available models")
    embed_p = sub._name_parser_map["embed"]
    embed_p.add_argument("--ollama-only", action="store_true",
                         help="Skip Bedrock models (useful when targeting a remote Ollama)")

    sub.add_parser("status", help="Show embedding progress per model")

    eval_p = sub.add_parser("eval", help="Evaluate models against a query set")
    eval_p.add_argument("query_set", help="Path to query set JSON file")

    args = parser.parse_args()

    global OLLAMA_URL, MODEL_TAG
    if args.ollama_url:
        OLLAMA_URL = args.ollama_url
    MODEL_TAG = args.tag

    if args.cmd == "status":
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT model_name, COUNT(*) FROM message_embeddings GROUP BY model_name ORDER BY model_name"
        )
        rows = cur.fetchall()
        conn.close()
        print(f"Total messages: {total}")
        print(f"\n{'Model':<35} {'Embedded':>10} {'%':>6}")
        print("-"*55)
        for model, count in rows:
            print(f"{model:<35} {count:>10} {100*count/total:>5.1f}%")
        known = {m for m, _ in rows}
        for model, _, _s, _w in MODELS:
            if model not in known:
                print(f"{model:<35} {'0':>10} {'0.0':>5}%")

    elif args.cmd == "embed":
        for model, dims, source, workers in MODELS:
            if getattr(args, "ollama_only", False) and source != "ollama":
                print(f"Skipping {model} — --ollama-only set")
                continue
            if not model_available(model, source):
                hint = f"ollama pull {model}" if source == "ollama" else f"check Bedrock access for {model}"
                print(f"Skipping {model} — not available ({hint})")
                continue
            run_embeddings(model, dims, source, max_workers=workers)

    elif args.cmd == "eval":
        evaluate(args.query_set)


if __name__ == "__main__":
    main()
