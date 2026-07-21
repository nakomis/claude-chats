#!/usr/bin/env python3
"""
Build a candidate query set for the embedding bake-off.

Uses the existing mxbai-embed-large embeddings to retrieve the top-N candidates
for each query. Output is a JSON file for human review — mark true positives,
discard false positives, add any IDs that were missed.
"""

import json
import sys
import urllib.request
import psycopg2

DB_URL = "postgresql://claude:claude@localhost:5433/claude_chats"
OLLAMA_URL = "http://localhost:11434"
MODEL = "mxbai-embed-large"
TOP_N = 20  # candidates per query for human review


def embed(text: str) -> list[float]:
    payload = json.dumps({"model": MODEL, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["embeddings"][0]


QUERIES = [
    "how did I fix the LUKS encrypted volumes on Leia after reinstalling",
    "Time Machine backup failing disconnected disk image on macOS",
    "CDK backup stack S3 bucket setup for Taiga",
    "Ansible playbook for Leia provisioning RAID setup",
    "conversation memory pgvector postgres backup to S3",
    "nginx reverse proxy letsencrypt certificate on Leia",
    "Taiga docker-compose postgres migration",
    "badblocks hard drive testing slow write cache",
    "Ubuntu autoinstall subiquity cloud-init configuration",
    "Samba SMB server configuration Time Machine vfs fruit",
    "Docker Compose service configuration home servers",
    "CDK IAM role OIDC GitHub Actions deployment",
    "Python asdf version management tensorflow",
    "pgvector embedding search cosine similarity",
    "Datadog observability logging container monitoring",
    "Let's Encrypt DNS Route53 certificate renewal",
    "RAID mdadm disk array configuration",
    "draw.io architecture diagram generation",
    "Taiga project management story sprint tracking",
    "SSH key authentication Leia Luke server access",
]


def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    query_set = []

    for i, query in enumerate(QUERIES):
        print(f"[{i+1}/{len(QUERIES)}] {query[:60]}...", flush=True)
        vec = embed(query)
        vec_str = str(vec)

        cur.execute(
            """
            SELECT
                m.id::text,
                m.role,
                left(m.content, 200) as snippet,
                (e.embedding <=> %s::vector) as distance
            FROM message_embeddings e
            JOIN messages m ON m.id = e.message_id
            WHERE e.model_name = %s
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
            """,
            (vec_str, MODEL, vec_str, TOP_N),
        )
        candidates = [
            {
                "id": row[0],
                "role": row[1],
                "snippet": row[2],
                "distance": round(row[3], 4),
                "relevant": None,  # human fills in: true / false / null
            }
            for row in cur.fetchall()
        ]

        query_set.append(
            {
                "query": query,
                "candidates": candidates,
                "relevant_ids": [],  # human fills in confirmed IDs
            }
        )

    conn.close()

    out_path = "/Users/nakomis/repos/nakomis/claude-chats/scripts/query_set_candidates.json"
    with open(out_path, "w") as f:
        json.dump(query_set, f, indent=2)

    print(f"\nWrote {len(query_set)} queries to {out_path}")
    print("Review each query's candidates, move confirmed IDs to relevant_ids.")
    print("Then produce a clean query_set.json with only relevant_ids for eval.")


if __name__ == "__main__":
    main()
