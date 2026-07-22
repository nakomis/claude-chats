"""
Backfill embeddings for messages that have none.

Used after restoring a backup produced *without* the embedding column
(see scripts/backup.sh, which excludes the ~144 MB of incompressible vectors
to keep dumps small). Re-embeds every message whose embedding IS NULL using the
same provider/model as the live hook (hook.embed — Ollama / mxbai-embed-large by
default), then rebuilds the ivfflat index so its centroids are trained on the
full, populated vector set rather than an empty table.

Idempotent: once every message with content has an embedding, a re-run is a
no-op. Config via the same env vars as the hook:
  CLAUDE_CHATS_DB_URL        (default postgresql://claude:claude@localhost:5433/claude_chats)
  CLAUDE_CHATS_PROVIDER/...  (see hook/hook/embed.py)
  CLAUDE_CHATS_BACKFILL_BATCH rows per commit (default 200)

Run it via:
  uv run --project hook backfill-embeddings
"""

import os
import sys
import time

DB_URL = os.environ.get(
    "CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats"
)
BATCH = int(os.environ.get("CLAUDE_CHATS_BACKFILL_BATCH", "200"))


def _vec_str(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


def main() -> None:
    import psycopg

    from hook.embed import MODEL, PROVIDER, get_embedding

    with psycopg.connect(DB_URL, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM messages WHERE embedding IS NULL AND content <> ''"
            )
            todo = cur.fetchone()[0]

        print(
            f"[backfill] {todo} messages need embeddings ({PROVIDER}/{MODEL})",
            file=sys.stderr,
        )
        if todo == 0:
            print("[backfill] nothing to do", file=sys.stderr)
            return

        done = 0
        # Rows that cannot be embedded at all must be excluded from the next
        # SELECT, not merely skipped. A skipped row keeps embedding IS NULL, so
        # it matches the query again on the following pass: with any permanently
        # unembeddable content the loop never terminates, never reaches the
        # REINDEX below, and hammers the embedding service indefinitely. Seen
        # 22 Jul 2026 with 14 rows of captured machine output (raw JSON tool
        # results, a null-byte dump) that make Ollama return HTTP 500.
        failed: set = set()
        t0 = time.perf_counter()
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, content FROM messages "
                    "WHERE embedding IS NULL AND content <> '' "
                    "AND NOT (id = ANY(%s)) LIMIT %s",
                    (list(failed), BATCH),
                )
                rows = cur.fetchall()
            if not rows:
                break
            with conn.cursor() as cur:
                for mid, content in rows:
                    try:
                        emb = get_embedding(content)
                    except Exception as exc:  # noqa: BLE001 — skip, keep going
                        print(f"[backfill] skip {mid}: {exc}", file=sys.stderr)
                        failed.add(mid)
                        continue
                    cur.execute(
                        "UPDATE messages SET embedding = %s::vector WHERE id = %s",
                        (_vec_str(emb), mid),
                    )
                    done += 1
            conn.commit()
            pct = done * 100 // max(todo, 1)
            print(f"[backfill] {done}/{todo} ({pct}%)", file=sys.stderr)

        if failed:
            print(
                f"[backfill] {len(failed)} message(s) could not be embedded and "
                "were left with NULL embeddings",
                file=sys.stderr,
            )

        # Rebuild the ivfflat index so its list centroids are trained on the full
        # vector set. Restoring loads all rows with NULL embeddings, so the index
        # created by init.sql starts empty; re-embedding alone would leave it
        # poorly trained. A plain REINDEX re-runs k-means over current data.
        print(
            "[backfill] rebuilding ivfflat index (messages_embedding_idx)",
            file=sys.stderr,
        )
        # ivfflat needs enough maintenance_work_mem to hold the sample it runs
        # k-means over, and that requirement grows with the corpus. At ~54k
        # vectors it wanted 80 MB against Postgres's 64 MB default and the
        # REINDEX aborted outright (22 Jul 2026), which would silently leave a
        # restored database with an untrained index. Raise it for this session
        # only; it is not a server-wide change.
        with conn.cursor() as cur:
            cur.execute("SET maintenance_work_mem = '512MB'")
            cur.execute("REINDEX INDEX messages_embedding_idx")
        conn.commit()

        dt = time.perf_counter() - t0
        print(
            f"[backfill] done: {done} embedded in {dt:.0f}s, index rebuilt",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
