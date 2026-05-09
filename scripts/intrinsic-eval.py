#!/usr/bin/env python3
"""
Intrinsic embedding evaluation (HOME-151).

Uses conversation structure as self-supervised ground truth: messages
from the same conversation should be more similar to each other than
to random messages from other conversations.

Metrics per model:
  recall@k       — fraction of top-k neighbours that share a conversation
  intra_sim      — mean cosine similarity between same-conversation pairs
  inter_sim      — mean cosine similarity between cross-conversation pairs
  separation     — intra_sim / inter_sim  (higher = better discrimination)

No human labels required.
"""

import json
import math
import random
import time
import psycopg2
import psycopg2.extras

DB_URL = "postgresql://claude:claude@localhost:5433/claude_chats"

MODELS = [
    "mxbai-embed-large",
    "nomic-embed-text:v1.5",
    "bge-m3",
    "bge-large",
    "snowflake-arctic-embed:l",
    "amazon.titan-embed-text-v2:0",
    "cohere.embed-english-v3",
    "cohere.embed-v4:0",
]

K = 10          # recall@k
N_PROBE = 300   # messages to probe for recall@k (keep runtime reasonable)
N_PAIRS = 1000  # pairs for intra/inter similarity


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def evaluate_model(conn, model: str) -> dict:
    cur = conn.cursor()

    # Check coverage
    cur.execute(
        "SELECT COUNT(*) FROM message_embeddings WHERE model_name = %s", (model,)
    )
    count = cur.fetchone()[0]
    if count == 0:
        return {"error": "not embedded yet"}

    print(f"\n{'='*60}")
    print(f"Model: {model}  ({count} embeddings)")

    # ── Recall@k ─────────────────────────────────────────────────────────────
    # Sample probe messages from conversations with ≥ K+1 messages
    print(f"  Computing recall@{K} on {N_PROBE} probe messages...")
    t0 = time.time()

    cur.execute(
        """
        SELECT e.message_id, m.conversation_id
        FROM message_embeddings e
        JOIN messages m ON m.id = e.message_id
        WHERE e.model_name = %s
          AND (
            SELECT COUNT(*) FROM messages m2
            WHERE m2.conversation_id = m.conversation_id
          ) > %s
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (model, K, N_PROBE),
    )
    probes = cur.fetchall()  # [(message_id, conversation_id), ...]

    recalls = []
    for msg_id, conv_id in probes:
        cur.execute(
            """
            SELECT m2.conversation_id
            FROM message_embeddings e2
            JOIN messages m2 ON m2.id = e2.message_id
            WHERE e2.model_name = %s
              AND e2.message_id != %s
            ORDER BY e2.embedding <=> (
                SELECT embedding FROM message_embeddings
                WHERE message_id = %s AND model_name = %s
            )
            LIMIT %s
            """,
            (model, msg_id, msg_id, model, K),
        )
        neighbours = cur.fetchall()
        same = sum(1 for (nconv,) in neighbours if nconv == conv_id)
        recalls.append(same / K)

    recall_at_k = sum(recalls) / len(recalls) if recalls else 0
    print(f"    recall@{K} = {recall_at_k:.4f}  ({time.time()-t0:.1f}s)")

    # ── Intra / inter similarity ──────────────────────────────────────────────
    # Positive pairs: consecutive messages in same conversation
    print(f"  Sampling {N_PAIRS} intra-conversation pairs...")
    t0 = time.time()

    cur.execute(
        """
        SELECT e1.embedding::text, e2.embedding::text
        FROM messages m1
        JOIN messages m2
          ON m2.conversation_id = m1.conversation_id
         AND m2.sequence_num = m1.sequence_num + 1
        JOIN message_embeddings e1 ON e1.message_id = m1.id AND e1.model_name = %s
        JOIN message_embeddings e2 ON e2.message_id = m2.id AND e2.model_name = %s
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (model, model, N_PAIRS),
    )
    intra_rows = cur.fetchall()
    intra_sims = [
        cosine_sim(json.loads(a), json.loads(b))
        for a, b in intra_rows
    ]
    intra_sim = sum(intra_sims) / len(intra_sims) if intra_sims else 0
    print(f"    intra_sim = {intra_sim:.4f}  ({len(intra_sims)} pairs, {time.time()-t0:.1f}s)")

    # Negative pairs: random messages from different conversations.
    # Sample two independent pools, pair them up in Python — avoids a
    # 29k×29k cross-join that takes hours with ORDER BY RANDOM().
    print(f"  Sampling {N_PAIRS} inter-conversation pairs...")
    t0 = time.time()

    sample_size = N_PAIRS * 2
    cur.execute(
        """
        SELECT e.message_id, m.conversation_id, e.embedding::text
        FROM message_embeddings e
        JOIN messages m ON m.id = e.message_id
        WHERE e.model_name = %s
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (model, sample_size),
    )
    pool = cur.fetchall()  # [(message_id, conv_id, embedding_text), ...]

    inter_sims = []
    seen = set()
    i, j = 0, len(pool) - 1
    while len(inter_sims) < N_PAIRS and i < j:
        mid_i, cid_i, emb_i = pool[i]
        mid_j, cid_j, emb_j = pool[j]
        if cid_i != cid_j and (mid_i, mid_j) not in seen:
            inter_sims.append(cosine_sim(json.loads(emb_i), json.loads(emb_j)))
            seen.add((mid_i, mid_j))
            i += 1
            j -= 1
        elif cid_i == cid_j:
            i += 1
        else:
            j -= 1
    inter_sim = sum(inter_sims) / len(inter_sims) if inter_sims else 0
    print(f"    inter_sim = {inter_sim:.4f}  ({len(inter_sims)} pairs, {time.time()-t0:.1f}s)")

    separation = intra_sim / inter_sim if inter_sim else 0

    result = {
        f"recall@{K}": round(recall_at_k, 4),
        "intra_sim": round(intra_sim, 4),
        "inter_sim": round(inter_sim, 4),
        "separation": round(separation, 4),
        "coverage": count,
    }
    print(f"    separation = {separation:.4f}")
    return result


def main():
    conn = psycopg2.connect(DB_URL)
    results = {}

    for model in MODELS:
        results[model] = evaluate_model(conn, model)

    conn.close()

    print("\n" + "=" * 70)
    print("INTRINSIC EVALUATION RESULTS")
    print("=" * 70)
    header = f"{'Model':<35} {f'R@{K}':>6} {'intra':>7} {'inter':>7} {'sep':>7} {'msgs':>7}"
    print(header)
    print("-" * 70)
    for model, r in sorted(results.items(), key=lambda x: -x[1].get(f"recall@{K}", 0)):
        if "error" in r:
            print(f"{model:<35}  {r['error']}")
        else:
            print(
                f"{model:<35} "
                f"{r[f'recall@{K}']:>6.4f} "
                f"{r['intra_sim']:>7.4f} "
                f"{r['inter_sim']:>7.4f} "
                f"{r['separation']:>7.4f} "
                f"{r['coverage']:>7}"
            )

    out = "/Users/nakomis/repos/nakomis/claude-chats/scripts/intrinsic_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {out}")


if __name__ == "__main__":
    main()
