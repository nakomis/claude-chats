#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg2-binary"]
# ///
"""
Re-embed conversation-memory rows in place with the live consumer's embedder.

Written for HOME-469: rows up to 2026-05-08 22:59 hold Titan v2 vectors, which
mxbai queries can't reach. General enough for the next one (HOME-435): pick rows
by date and by "not already produced by the target embedder", embed them the way
Cal's consumer does, and write the vector back with provenance.

Matches cal/embedding-consumer (home-infra):
  * model `mxbai-q8` (mxbai-embed-large quantised to Q8_0). Ollama 0.40 on a Mac
    won't quantise a GGUF at create time, so build it with llama.cpp:
        llama-quantize <mxbai-embed-large blob> mxbai-q8.gguf Q8_0
        ollama create mxbai-q8 -f Modelfile   # FROM ./mxbai-q8.gguf + num_ctx 512
    Phi vs Cal agree to cos 0.9999 (Metal vs CPU arithmetic).
  * input cut to 800 characters (code points, like the consumer's char_indices),
    then `/api/embed` with truncate=true so token-dense text clamps server-side.
  * `embedder` = ollama-mxbai-q8, `embedder_board` = the machine that did it.

Resumable and idempotent: each batch re-selects rows whose embedder isn't the
target yet, so a re-run picks up where it stopped. Commits every --batch rows to
keep transactions short (Luke's playbook reapplies init.sql with lock_timeout=5s
and needs ACCESS EXCLUSIVE briefly). Refuses to run across the nightly backup
(02:30 UTC) so a month is never archived half re-embedded.

BEFORE RUNNING: the HOME-458 archive must fingerprint the embedder distribution
(the `e` key fact), or archived months keep their old vectors forever. Pass
--archive-ok once that is merged and deployed to Luke.

  uv run scripts/reembed.py --before '2026-05-08 23:00' --dry-run
  uv run scripts/reembed.py --before '2026-05-08 23:00' --archive-ok
"""

import argparse
import datetime as dt
import json
import socket
import sys
import time
import urllib.request

import psycopg2

DB_URL = "postgresql://claude:claude@luke.local:5432/claude_chats"
TAG = "ollama-mxbai-q8"
MAX_CHARS = 800
BACKUP_UTC = dt.time(2, 30)
QUIET = dt.timedelta(minutes=30)  # stay this far either side of the nightly


def log(msg: str) -> None:
    print(f"[reembed {dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def near_backup(now: dt.datetime) -> bool:
    start = now.replace(hour=BACKUP_UTC.hour, minute=BACKUP_UTC.minute, second=0, microsecond=0)
    return any(abs(now - (start + dt.timedelta(days=d))) < QUIET for d in (-1, 0, 1))


def embed(ollama: str, model: str, texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": model, "input": texts, "truncate": True}).encode()
    req = urllib.request.Request(f"{ollama}/api/embed", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        vecs = json.load(resp)["embeddings"]
    if len(vecs) != len(texts) or any(len(v) != 1024 for v in vecs):
        raise RuntimeError("unexpected embedding shape")
    return vecs


def vec_str(v: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in v) + "]"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", required=True, help="re-embed rows with created_at before this")
    ap.add_argument("--after", default="-infinity", help="...and at or after this")
    ap.add_argument("--only-untagged", action="store_true", default=True,
                    help="only rows with embedder IS NULL (default; HOME-469's Titan rows are untagged)")
    ap.add_argument("--any-other-embedder", dest="only_untagged", action="store_false",
                    help="re-embed any row not already tagged with the target embedder")
    ap.add_argument("--db", default=DB_URL)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="mxbai-q8")
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--board", default=socket.gethostname().split(".")[0].lower())
    ap.add_argument("--batch", type=int, default=2000, help="rows per commit")
    ap.add_argument("--embed-batch", type=int, default=32, help="texts per Ollama request")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--archive-ok", action="store_true",
                    help="confirm the HOME-458 archiver fingerprints the embedder (see docstring)")
    ap.add_argument("--no-reindex", action="store_true")
    args = ap.parse_args()

    where = "created_at >= %s AND created_at < %s AND content <> '' AND " + (
        "embedder IS NULL" if args.only_untagged else "embedder IS DISTINCT FROM %s")
    params = [args.after, args.before] + ([] if args.only_untagged else [args.tag])

    conn = psycopg2.connect(args.db)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*), min(created_at), max(created_at) FROM messages WHERE {where}", params)
        todo, lo, hi = cur.fetchone()
    conn.commit()
    log(f"{todo} rows to re-embed ({lo} .. {hi}) with {args.model} -> {args.tag} on {args.board}")
    if args.dry_run or todo == 0:
        return
    if not args.archive_ok:
        sys.exit("refusing: pass --archive-ok once the HOME-458 archiver fingerprints the embedder")

    done, failed, t0 = 0, [], time.perf_counter()
    while True:
        now = dt.datetime.now(dt.UTC)
        if near_backup(now):
            log("within 30 min of the 02:30 UTC backup; sleeping 5 min")
            time.sleep(300)
            continue
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT message_uuid, content FROM messages WHERE {where} "
                "AND NOT (message_uuid = ANY(%s)) ORDER BY created_at, message_uuid LIMIT %s",
                params + [failed, args.batch])
            rows = cur.fetchall()
        conn.commit()
        if not rows:
            break
        out = []
        for i in range(0, len(rows), args.embed_batch):
            chunk = rows[i:i + args.embed_batch]
            texts = [c[:MAX_CHARS] for _, c in chunk]
            try:
                vecs = embed(args.ollama, args.model, texts)
            except Exception:  # noqa: BLE001 - retry one by one to isolate the bad row
                vecs = []
                for (uuid, _), t in zip(chunk, texts):
                    try:
                        vecs.append(embed(args.ollama, args.model, [t])[0])
                    except Exception as exc:  # noqa: BLE001 - skip and keep going
                        log(f"skip {uuid}: {exc}")
                        failed.append(uuid)
                        vecs.append(None)
            out += [(vec_str(v), args.tag, args.board, u) for (u, _), v in zip(chunk, vecs) if v]
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE messages SET embedding = %s::vector, embedder = %s, embedder_board = %s, "
                "embedder_fw = NULL WHERE message_uuid = %s", out)
        conn.commit()
        done += len(out)
        rate = done / (time.perf_counter() - t0)
        log(f"{done}/{todo} ({done * 100 // todo}%) {rate:.0f}/s, ~{(todo - done) / rate / 60:.0f} min left")

    if failed:
        log(f"{len(failed)} row(s) could not be embedded: {failed[:10]}")
    log(f"embedded {done} in {time.perf_counter() - t0:.0f}s")
    if args.no_reindex:
        return

    # CONCURRENTLY so the consumer's inserts aren't blocked for the whole build
    # (the index is ~1 GB); it can't run in a transaction block.
    conn.autocommit = True
    with conn.cursor() as cur:
        log("REINDEX INDEX CONCURRENTLY messages_embedding_idx")
        cur.execute("SET maintenance_work_mem = '1GB'")
        cur.execute("REINDEX INDEX CONCURRENTLY messages_embedding_idx")
        # The UPDATEs write new tuple versions wherever there's room, which can
        # weaken the BRIN index on created_at (HOME-464); report its correlation.
        log("VACUUM (ANALYZE) messages")
        cur.execute("VACUUM (ANALYZE) messages")
        cur.execute("SELECT correlation FROM pg_stats WHERE tablename = 'messages' AND attname = 'created_at'")
        row = cur.fetchone()
        log(f"created_at correlation after: {row[0] if row else 'n/a'} (was 0.980)")


if __name__ == "__main__":
    main()
