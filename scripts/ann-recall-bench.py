#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]"]
# ///
"""
Recall and latency of the semantic search query against ivfflat.probes (CHATS-15).

Embeds each query in scripts/query_set.json (plus any --extra file of the same
shape) with mxbai-q8, the way the MCP does, then runs the MCP's vector query
(join on conversations, iterative_scan = relaxed_order) once as an exact scan
and once per --probes value. Recall@k is the share of the exact top k each
setting returns. Run with and without the MCP's default 3-month window.

Also prints the top plan node per setting. pgvector's cost estimate is not
monotonic in probes: on Luke at 123k rows, probes=5 flipped the planner onto a
parallel seq scan (8-24 s) while 4 and 10 stayed on the index. Check the plan
column before changing IVFFLAT_PROBES.

Measured 16 Oct 2026, Luke, 123,481 rows, lists=100, 26 queries, k=10:
  exact     p50 2.1 s   recall 1
  probes=1  p50 0.02 s  recall 0.58 / 0.57 (3 months)
  probes=10 p50 0.12 s  recall 0.94 / 0.95
  probes=20 p50 0.23 s  recall 0.97 / 0.99

  uv run scripts/ann-recall-bench.py
  uv run scripts/ann-recall-bench.py -k 50 --probes 1 10 20 40
"""

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

import psycopg

EMBED_MAX_CHARS = 800  # Cal's consumer and the MCP cut input here
SQL = """SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id
         WHERE m.embedding IS NOT NULL {window}
         ORDER BY m.embedding <=> %s::vector LIMIT %s"""
WINDOW = "AND m.created_at >= now() - interval '3 months'"


def embed(ollama: str, model: str, text: str) -> str:
    req = urllib.request.Request(
        f"{ollama}/api/embed",
        json.dumps({"model": model, "input": [text[:EMBED_MAX_CHARS]], "truncate": True}).encode(),
        {"Content-Type": "application/json"},
    )
    vec = json.loads(urllib.request.urlopen(req).read())["embeddings"][0]
    return "[" + ",".join(map(str, vec)) + "]"


def plan_node(cur, sql: str, params) -> str:
    """How the planner reaches messages: through the vector index, or not."""
    cur.execute("EXPLAIN " + sql, params)
    plan = "\n".join(r[0] for r in cur.fetchall())
    for line in plan.splitlines():
        if "Index Scan using" in line and "embedding" in line:
            return line.strip().lstrip("-> ").split("  (")[0]
    return "SEQ SCAN (no vector index)" if "Seq Scan on messages" in plan else "?"


def main() -> None:
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="postgresql://claude:claude@luke.local:5432/claude_chats")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="mxbai-q8")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--probes", type=int, nargs="+", default=[1, 10, 15, 20])
    ap.add_argument("--queries", type=Path, default=root / "query_set.json")
    ap.add_argument("--extra", type=Path, help="more queries, same shape as query_set.json")
    args = ap.parse_args()

    queries = [q["query"] for q in json.loads(args.queries.read_text())]
    if args.extra:
        queries += [q["query"] for q in json.loads(args.extra.read_text())]
    print(f"embedding {len(queries)} queries with {args.model} ...", flush=True)
    vecs = [embed(args.ollama, args.model, q) for q in queries]

    with psycopg.connect(args.db, autocommit=True) as conn:
        cur = conn.cursor()
        # Load pgvector's GUCs first, or the SETs below land on placeholders.
        cur.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
        cur.execute("SET ivfflat.iterative_scan = relaxed_order")
        cur.execute("SELECT count(*) FROM messages WHERE embedding IS NOT NULL")
        print(f"{cur.fetchone()[0]:,} embedded rows, k={args.k}\n")
        print(f"{'window':<7}{'config':<11}{'p50':>8}{'max':>8}  {'recall':<20}plan")

        for wname, window in (("all", ""), ("3 mo", WINDOW)):
            sql = SQL.format(window=window)
            truth: list[set] = []
            for probes in [None, *args.probes]:
                if probes is None:
                    cur.execute("SET enable_indexscan = off")
                else:
                    cur.execute("SET enable_indexscan = on")
                    cur.execute(f"SET ivfflat.probes = {int(probes)}")
                times, recalls = [], []
                for i, v in enumerate(vecs):
                    t0 = time.perf_counter()
                    cur.execute(sql, (v, args.k))
                    ids = {r[0] for r in cur.fetchall()}
                    times.append(time.perf_counter() - t0)
                    if probes is None:
                        truth.append(ids)
                    else:
                        recalls.append(len(truth[i] & ids) / args.k)
                rec = f"{statistics.mean(recalls):.3f} (min {min(recalls):.1f})" if recalls else "1 (truth)"
                name = "exact" if probes is None else f"probes={probes}"
                print(f"{wname:<7}{name:<11}{statistics.median(times):7.2f}s{max(times):7.2f}s  "
                      f"{rec:<20}{plan_node(cur, sql, (vecs[0], args.k))}", flush=True)
        cur.execute("RESET enable_indexscan")


if __name__ == "__main__":
    main()
