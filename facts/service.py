# Copyright 2026 Martin Harris
# Licensed under the Apache License, Version 2.0; see facts/LICENSE.
"""The facts extractor service (CHATS-25): new exchanges in, facts out. One run per invocation.

    python -m facts.service [--limit N] [--dry-run]

Meant to be fired by a systemd timer every ~15 minutes. Each run:

1. Finds conversations with messages past their facts_progress.last_seq and splits
   them into exchanges with the same rules the eval harness uses
   (facts_eval.export.split_exchanges). An exchange is only processed once it is
   complete: a later message of Martin's exists, or the session has been idle for
   FACTS_IDLE_MINUTES.
2. Runs Extractor.extract on each, in order, and inserts the facts and advances
   facts_progress in ONE transaction, so a crash or a rerun never duplicates or skips.
   An exchange whose extraction fails is left unprocessed and retried next run, up to
   FACTS_MAX_ATTEMPTS times; after that it is logged and skipped so one poison exchange
   cannot block its conversation.
3. Embeds facts that have no vector yet, the way CM embeds messages (Ollama, mxbai-q8,
   800 characters, truncate=true) and tags them in facts.embedder.
4. Prints one `facts-run {...}` JSON line of counts (exchanges, facts, errors, tokens).

Configuration is environment only; see Config. Raw prompts and model replies are never
stored and never logged. --dry-run calls the model (that is the point) but writes
nothing to the database and embeds nothing; it prints the facts it would have stored.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

from facts.extract import ALREADY_EXTRACTED, EARLIER_MESSAGES, Extractor, observation_date
from facts_eval.export import MAX_CHARS, split_exchanges

DEFAULT_KEY_PARAM = "/conversation-memory/prod/facts/anthropic-api-key"
LOCK_KEY = 0x66616374  # "fact": one service run at a time per database
DEFAULT_KIND = "situation"  # for the rare fact the model gave no valid kind


@dataclass
class Config:
    dsn: str = ""
    embed_url: str = ""               # Ollama base URL, e.g. http://host:11434
    embed_model: str = "mxbai-q8"
    embed_tag: str = "ollama-mxbai-q8"  # facts.embedder; same value Cal's consumer writes on messages
    embed_max_chars: int = 800         # the canonical truncation: queries and messages use it too
    embed_dims: int = 1024
    embed_batch: int = 200
    max_exchanges: int = 40            # per run; the next timer tick carries on
    idle_minutes: int = 30
    max_attempts: int = 3
    max_runtime_seconds: int = 780     # stop starting new exchanges after this (timer is every 15 min)
    since: str = ""                    # ignore messages before this ISO date/time (no backfill of history)
    model: str = ""                    # blank = the Extractor's default

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        d = cls()
        return cls(
            dsn=env.get("FACTS_DATABASE_URL") or env.get("CM_DATABASE_URL", ""),
            embed_url=(env.get("FACTS_EMBED_URL") or env.get("OLLAMA_BASE_URL", "")).rstrip("/"),
            embed_model=env.get("FACTS_EMBED_MODEL", d.embed_model),
            embed_tag=env.get("FACTS_EMBED_TAG", d.embed_tag),
            embed_max_chars=int(env.get("FACTS_EMBED_MAX_CHARS", d.embed_max_chars)),
            embed_dims=int(env.get("FACTS_EMBED_DIMS", d.embed_dims)),
            embed_batch=int(env.get("FACTS_EMBED_BATCH", d.embed_batch)),
            max_exchanges=int(env.get("FACTS_MAX_EXCHANGES", d.max_exchanges)),
            idle_minutes=int(env.get("FACTS_IDLE_MINUTES", d.idle_minutes)),
            max_attempts=int(env.get("FACTS_MAX_ATTEMPTS", d.max_attempts)),
            max_runtime_seconds=int(env.get("FACTS_MAX_RUNTIME_SECONDS", d.max_runtime_seconds)),
            since=env.get("FACTS_SINCE", ""),
            model=env.get("FACTS_MODEL", ""),
        )


@dataclass
class Stats:
    conversations: int = 0
    exchanges: int = 0
    facts: int = 0
    dropped: int = 0          # items the extractor rejected (credential-shaped, malformed)
    errors: int = 0
    gave_up: int = 0
    skipped_open: int = 0     # exchanges still in progress, left for a later run
    embedded: int = 0
    embed_errors: int = 0
    uncached_in: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    dry_run: bool = False
    seconds: float = 0.0

    def add_tokens(self, calls):
        for c in calls:
            self.uncached_in += c.get("uncached_in") or 0
            self.cache_read += c.get("cache_read") or 0
            self.cache_write += c.get("cache_write") or 0
            self.output_tokens += c.get("output_tokens") or 0


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def anthropic_api_key(env=None, ssm_client=None):
    """$ANTHROPIC_API_KEY, else the SecureString named by $FACTS_ANTHROPIC_KEY_PARAM from SSM."""
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY"):
        return env["ANTHROPIC_API_KEY"]
    name = env.get("FACTS_ANTHROPIC_KEY_PARAM", DEFAULT_KEY_PARAM)
    if ssm_client is None:
        import boto3

        ssm_client = boto3.client("ssm", region_name=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "eu-west-2")
    return ssm_client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def make_extractor(cfg, env=None):
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key(env), max_retries=6)
    return Extractor(client, **({"model": cfg.model} if cfg.model else {}))


def make_embedder(cfg):
    """text -> vector, the way Cal's embedding-consumer does it (cal/embedding-consumer/src/ollama.rs)."""
    import httpx

    def embed(text):
        r = httpx.post(f"{cfg.embed_url}/api/embed", timeout=120.0,
                       json={"model": cfg.embed_model, "input": text[:cfg.embed_max_chars], "truncate": True})
        r.raise_for_status()
        vec = r.json()["embeddings"][0]
        if len(vec) != cfg.embed_dims:
            raise ValueError(f"embedding has {len(vec)} dimensions, expected {cfg.embed_dims}")
        return vec

    return embed


# ---- database ---------------------------------------------------------------------

def pending_conversations(conn, cfg):
    """(id, host, last_seq, fail_seq, fail_count, idle) for each conversation with unprocessed messages."""
    return conn.execute(
        """
        SELECT c.id, c.host, COALESCE(p.last_seq, -1), p.fail_seq, COALESCE(p.fail_count, 0),
               (SELECT now() - max(m.created_at) FROM messages m WHERE m.conversation_id = c.id)
                   >= make_interval(mins => %(idle)s)
          FROM conversations c
          LEFT JOIN facts_progress p ON p.conversation_id = c.id
         WHERE EXISTS (SELECT 1 FROM messages m
                        WHERE m.conversation_id = c.id AND m.author IN ('martin', 'claude')
                          AND m.sequence_num > COALESCE(p.last_seq, -1)
                          AND m.created_at >= COALESCE(NULLIF(%(since)s, '')::timestamptz, '-infinity'))
         ORDER BY c.started_at, c.id
        """,
        {"idle": cfg.idle_minutes, "since": cfg.since},
    ).fetchall()


def pending_exchanges(conn, cfg, conv_id, last_seq):
    rows = conn.execute(
        """
        SELECT author, content, created_at, sequence_num FROM messages
         WHERE conversation_id = %(c)s AND author IN ('martin', 'claude') AND sequence_num > %(seq)s
           AND created_at >= COALESCE(NULLIF(%(since)s, '')::timestamptz, '-infinity')
         ORDER BY sequence_num
        """,
        {"c": conv_id, "seq": last_seq, "since": cfg.since},
    ).fetchall()
    return split_exchanges(rows)


def earlier_messages(conn, conv_id, before_seq):
    rows = conn.execute(
        """
        SELECT author, content, created_at, sequence_num FROM messages
         WHERE conversation_id = %s AND author IN ('martin', 'claude') AND sequence_num < %s
         ORDER BY sequence_num DESC LIMIT %s
        """,
        (conv_id, before_seq, EARLIER_MESSAGES * 3),
    ).fetchall()
    # Same dedupe and truncation as the exchanges, but flat: the window need not start at Martin.
    flat, prev = [], None
    for author, content, *_ in rows[::-1]:
        if (author, content) != prev:
            flat.append({"role": "user" if author == "martin" else "assistant", "content": content[:MAX_CHARS]})
        prev = (author, content)
    return flat[-EARLIER_MESSAGES:]


def recent_facts(conn, conv_id):
    rows = conn.execute(
        "SELECT text FROM facts WHERE conversation_id = %s AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT %s", (conv_id, ALREADY_EXTRACTED)).fetchall()
    return [r[0] for r in rows][::-1]


def commit_exchange(conn, ex, conv_id, host, version, facts, day=None):
    """The exchange's facts and the progress advance, in one transaction."""
    with conn.transaction():
        for f in facts:
            conn.execute(
                """
                INSERT INTO facts (text, kind, subject, attribute, observed_at, last_confirmed, temporary,
                                   expected_until, conversation_id, first_seq, last_seq, host, extractor)
                VALUES (%(text)s, %(kind)s, %(subject)s, %(attribute)s, %(at)s, %(at)s, %(temporary)s,
                        %(until)s::date, %(conv)s, %(first)s, %(last)s, %(host)s, %(extractor)s)
                """,
                {"text": f.text, "kind": f.kind or DEFAULT_KIND, "subject": f.subject, "attribute": f.attribute,
                 "at": ex.started_at, "temporary": f.temporary, "until": f.expected_until, "conv": conv_id,
                 "first": ex.first_seq, "last": ex.last_seq, "host": host, "extractor": version})
        advance(conn, conv_id, ex.last_seq)


def advance(conn, conv_id, last_seq, gave_up=False):
    conn.execute(
        """
        INSERT INTO facts_progress (conversation_id, last_seq, gave_up) VALUES (%(c)s, %(s)s, %(g)s)
        ON CONFLICT (conversation_id) DO UPDATE
           SET last_seq = GREATEST(facts_progress.last_seq, EXCLUDED.last_seq),
               fail_seq = NULL, fail_count = 0,
               gave_up = facts_progress.gave_up + EXCLUDED.gave_up, updated_at = now()
        """, {"c": conv_id, "s": last_seq, "g": 1 if gave_up else 0})


def record_failure(conn, conv_id, last_seq, first_seq, prior_fail_seq, prior_count):
    """Count one more failure of the exchange starting at first_seq; returns the new count."""
    count = prior_count + 1 if prior_fail_seq == first_seq else 1
    conn.execute(
        """
        INSERT INTO facts_progress (conversation_id, last_seq, fail_seq, fail_count) VALUES (%(c)s, %(s)s, %(f)s, %(n)s)
        ON CONFLICT (conversation_id) DO UPDATE SET fail_seq = %(f)s, fail_count = %(n)s, updated_at = now()
        """, {"c": conv_id, "s": last_seq, "f": first_seq, "n": count})
    return count


# ---- the run ----------------------------------------------------------------------

def extract_pending(conn, cfg, extractor, stats, limit, dry_run, out=print):
    started = time.monotonic()
    dry_seen = {}
    for conv_id, host, last_seq, fail_seq, fail_count, idle in pending_conversations(conn, cfg):
        if stats.exchanges + stats.errors >= limit or time.monotonic() - started > cfg.max_runtime_seconds:
            break
        stats.conversations += 1
        for ex in pending_exchanges(conn, cfg, conv_id, last_seq):
            if not ex.closed and not idle:
                stats.skipped_open += 1
                break  # the last one: Martin's next message has not come and the session is not idle yet
            if stats.exchanges + stats.errors >= limit or time.monotonic() - started > cfg.max_runtime_seconds:
                break
            earlier = earlier_messages(conn, conv_id, ex.first_seq)
            already = (recent_facts(conn, conv_id) + dry_seen.get(conv_id, []))[-ALREADY_EXTRACTED:]
            res = extractor.extract(ex.messages, observation_date(ex.started_at), earlier, already)
            stats.add_tokens(res.calls)
            error = res.error
            if not error and not dry_run:
                try:
                    commit_exchange(conn, ex, conv_id, host, extractor.version, res.facts)
                except Exception as e:  # noqa: BLE001 - the transaction rolled back; counts as a failed attempt
                    error = f"database: {type(e).__name__}: {e}"
            if error:
                stats.errors += 1
                log(f"conversation {conv_id} seq {ex.first_seq}-{ex.last_seq}: failed: {error[:200]}")
                if dry_run:
                    break
                n = record_failure(conn, conv_id, last_seq, ex.first_seq, fail_seq, fail_count)
                if n >= cfg.max_attempts:
                    advance(conn, conv_id, ex.last_seq, gave_up=True)
                    stats.gave_up += 1
                    log(f"conversation {conv_id} seq {ex.first_seq}-{ex.last_seq}: giving up after {n} attempts")
                    last_seq, fail_seq, fail_count = ex.last_seq, None, 0
                    continue
                break  # retry this exchange next run, and keep the rest of the conversation in order
            stats.exchanges += 1
            stats.facts += len(res.facts)
            stats.dropped += len(res.dropped)
            if dry_run:
                out(f"[dry-run] {conv_id} seq {ex.first_seq}-{ex.last_seq} ({observation_date(ex.started_at)}): "
                    f"{len(res.facts)} fact(s)")
                for f in res.facts:
                    out(f"    [{f.kind}] {f.subject}/{f.attribute}: {f.text}")
                dry_seen.setdefault(conv_id, []).extend(f.text for f in res.facts)
            last_seq, fail_seq, fail_count = ex.last_seq, None, 0


def embed_pending(conn, cfg, embed, stats):
    rows = conn.execute(
        "SELECT id, text FROM facts WHERE embedding IS NULL AND deleted_at IS NULL ORDER BY created_at LIMIT %s",
        (cfg.embed_batch,)).fetchall()
    consecutive = 0
    for fact_id, text in rows:
        try:
            vec = embed(text)
        except Exception as e:  # noqa: BLE001 - retried next run
            stats.embed_errors += 1
            consecutive += 1
            log(f"fact {fact_id}: embedding failed: {str(e)[:200]}")
            if consecutive >= 3:
                log("three embedding failures in a row; leaving the rest for the next run")
                break
            continue
        consecutive = 0
        conn.execute(
            "UPDATE facts SET embedding = %s::vector, embedder = %s, embedder_board = NULL, embedder_fw = NULL, "
            "updated_at = now() WHERE id = %s AND embedding IS NULL",
            ("[" + ",".join(repr(float(x)) for x in vec) + "]", cfg.embed_tag, fact_id))
        stats.embedded += 1


def run(conn, cfg, extractor, embed, limit=None, dry_run=False, out=print):
    """One service run over an open autocommit connection. Returns Stats; None if another run holds the lock."""
    stats = Stats(dry_run=dry_run)
    t = time.monotonic()
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
        log("another facts run holds the lock; nothing to do")
        return None
    try:
        extract_pending(conn, cfg, extractor, stats, limit or cfg.max_exchanges, dry_run, out)
        if not dry_run:
            embed_pending(conn, cfg, embed, stats)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    stats.seconds = round(time.monotonic() - t, 1)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract facts from new conversation-memory exchanges.")
    ap.add_argument("--limit", type=int, help="process at most this many exchanges (default FACTS_MAX_EXCHANGES)")
    ap.add_argument("--dry-run", action="store_true",
                    help="call the model but write nothing to the database and embed nothing; print the facts")
    args = ap.parse_args(argv)

    import psycopg

    cfg = Config.from_env()
    if not cfg.dsn:
        sys.exit("FACTS_DATABASE_URL (or CM_DATABASE_URL) is not set")
    if not cfg.embed_url and not args.dry_run:
        sys.exit("FACTS_EMBED_URL (or OLLAMA_BASE_URL) is not set")
    extractor = make_extractor(cfg)
    embed = make_embedder(cfg) if cfg.embed_url else None
    with psycopg.connect(cfg.dsn, autocommit=True, connect_timeout=15) as conn:
        stats = run(conn, cfg, extractor, embed, args.limit, args.dry_run)
    if stats is not None:
        print("facts-run " + json.dumps(stats.__dict__, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
