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
3. Embeds facts that have no vector yet, the way CM embeds messages, and tags them in
   facts.embedder. Distributed edition (FACTS_EMBEDDER=ollama, the default): Cal's
   Ollama, mxbai-q8, 800 characters, truncate=true. Single machine (FACTS_EMBEDDER=hook):
   hook.embed with the provider install.sh recorded, which is what embeds the queries.
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
from dataclasses import dataclass, replace
from datetime import datetime

from facts.extract import ALREADY_EXTRACTED, EARLIER_MESSAGES, Extractor, is_systemic, observation_date
from facts_eval.export import MAX_CHARS, split_exchanges

DEFAULT_KEY_PARAM = "/conversation-memory/prod/facts/anthropic-api-key"
PROVIDERS = ("anthropic", "claude-code", "bedrock", "off")
EMBEDDERS = ("ollama", "hook")
# Claude Haiku 5.5 on Bedrock is only reachable through an inference profile. The global
# one works from any region; FACTS_MODEL=eu.anthropic.claude-haiku-5-5 (or us.) keeps the
# request in one geography.
BEDROCK_DEFAULT_MODEL = "global.anthropic.claude-haiku-5-5"
LOCK_KEY = 0x66616374  # "fact": one service run at a time per database
DEFAULT_KIND = "situation"  # for the rare fact the model gave no valid kind


class ConfigError(Exception):
    """A setting that is missing or unusable; the message says what to do."""


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
    # Ignore messages before this ISO date/time. Required for a real run: empty is an error, and
    # the whole history is processed only when it is explicitly "all" (paid calls for every exchange).
    since: str = ""
    max_systemic: int = 3              # consecutive transport/auth errors that stop the run
    api_timeout: float = 120.0         # seconds, per Anthropic request or claude -p run
    statement_timeout_ms: int = 60000  # per SQL statement
    model: str = ""                    # blank = the Extractor's default
    # anthropic: the API (the distributed service); claude-code: `claude -p` on the user's own
    # login; bedrock: Haiku on Bedrock with the AWS credentials in the environment; off (CHATS-39).
    provider: str = "anthropic"
    # Conversations with unprocessed messages from this ISO time on go first, so a backfill of
    # older history (FACTS_SINCE earlier than this) never holds up new conversations. Empty: oldest first.
    priority_since: str = ""
    # message (default): FACTS_SINCE filters messages by time. conversation: it selects whole
    # conversations by when they started, read from their first message (the single machine), so
    # a later, earlier FACTS_SINCE (a backfill) never finds a conversation half-read.
    since_scope: str = "message"
    effort: str = "low"                # "" or "none": send no effort (models that do not take one)
    # ollama: POST {embed_url}/api/embed as Cal's consumer does (the distributed edition).
    # hook: hook.embed with CLAUDE_CHATS_PROVIDER/MODEL, as the single-machine edition embeds queries.
    embedder: str = "ollama"

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
            since=env.get("FACTS_SINCE", "").strip(),
            max_systemic=int(env.get("FACTS_MAX_SYSTEMIC_ERRORS", d.max_systemic)),
            api_timeout=float(env.get("FACTS_API_TIMEOUT_SECONDS", d.api_timeout)),
            statement_timeout_ms=int(env.get("FACTS_STATEMENT_TIMEOUT_MS", d.statement_timeout_ms)),
            model=env.get("FACTS_MODEL", ""),
            provider=env.get("FACTS_PROVIDER", d.provider).strip() or d.provider,
            effort=env.get("FACTS_EFFORT", d.effort).strip(),
            priority_since=env.get("FACTS_PRIORITY_SINCE", "").strip(),
            since_scope=env.get("FACTS_SINCE_SCOPE", "message").strip() or "message",
            embedder=env.get("FACTS_EMBEDDER", d.embedder).strip() or d.embedder,
        )


def since_sql(cfg):
    """The FACTS_SINCE value as the queries take it: '' = no lower bound. Raises ConfigError if unusable."""
    if not cfg.since:
        raise ConfigError("FACTS_SINCE is not set. Give an ISO date or time (messages before it are ignored), "
                          "or FACTS_SINCE=all to process the whole history deliberately.")
    if cfg.since_scope not in ("message", "conversation"):
        raise ConfigError(f"FACTS_SINCE_SCOPE={cfg.since_scope!r}: want message or conversation")
    if cfg.since.lower() == "all":
        return ""
    try:
        datetime.fromisoformat(cfg.since)
    except ValueError:
        raise ConfigError(f"FACTS_SINCE={cfg.since!r} is not an ISO date or time (or 'all')") from None
    return cfg.since


def priority_sql(cfg):
    """FACTS_PRIORITY_SINCE as the query takes it: '' = none. Raises ConfigError if unusable."""
    if not cfg.priority_since:
        return ""
    try:
        datetime.fromisoformat(cfg.priority_since)
    except ValueError:
        raise ConfigError(f"FACTS_PRIORITY_SINCE={cfg.priority_since!r} is not an ISO date or time") from None
    return cfg.priority_since


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
    systemic_errors: int = 0  # outage-type failures (not counted as attempts at an exchange)
    aborted: bool = False     # the run stopped early on consecutive systemic errors

    @property
    def failed(self):
        """Errors and nothing to show for the run: the exit status that makes systemd's unit fail."""
        return (self.errors + self.embed_errors) > 0 and self.exchanges + self.embedded == 0

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
    effort = None if cfg.effort.lower() in ("", "none") else cfg.effort
    if cfg.provider == "claude-code":
        from facts.claude_code import ClaudeCodeExtractor

        # No API key at all: the user's Claude Code login pays. api_timeout bounds each claude -p run.
        return ClaudeCodeExtractor(timeout=cfg.api_timeout, env=env, effort=effort or "low",
                                   **({"model": cfg.model} if cfg.model else {}))
    if cfg.provider == "bedrock":
        import anthropic

        env = os.environ if env is None else env
        # Credentials the AWS way (AWS_PROFILE, SSO, instance role ...); the region from the
        # environment, else the profile's, else the SDK's default.
        client = anthropic.AnthropicBedrock(
            aws_profile=env.get("AWS_PROFILE") or None,
            aws_region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None,
            max_retries=6, timeout=cfg.api_timeout)
        return Extractor(client, model=cfg.model or BEDROCK_DEFAULT_MODEL, effort=effort)
    if cfg.provider != "anthropic":
        raise ConfigError(f"FACTS_PROVIDER={cfg.provider!r}: want one of {', '.join(PROVIDERS)}")
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key(env), max_retries=6, timeout=cfg.api_timeout)
    return Extractor(client, effort=effort, **({"model": cfg.model} if cfg.model else {}))


def make_hook_embedder(cfg, env=None):
    """(cfg with its embedder tag, text -> vector) through hook.embed, as the single-machine edition
    embeds messages and queries, so facts and the queries that search them share a vector space.

    hook.embed reads CLAUDE_CHATS_PROVIDER / _MODEL / _DIMENSIONS when it is first imported,
    so the environment must be complete before this is called.
    """
    from hook import embed as hook_embed

    env = os.environ if env is None else env
    if not env.get("FACTS_EMBED_TAG"):
        cfg = replace(cfg, embed_tag=f"{hook_embed.PROVIDER}-{hook_embed.MODEL}")

    def embed(text):
        vec = hook_embed.get_embedding(text)
        if len(vec) != cfg.embed_dims:
            raise ValueError(f"embedding has {len(vec)} dimensions, expected {cfg.embed_dims}")
        return vec

    return cfg, embed


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
    """(id, host, last_seq, fail_seq, fail_count, idle) for each conversation with unprocessed messages.

    A conversation whose tail cannot (yet) form an exchange, such as a last message of Martin's that
    Claude never answered, is selected again every run. That is deliberate and cheap: progress is not
    advanced past it, because a late reply would then be lost, and each re-selection costs one indexed
    read of that conversation's few unprocessed rows and no model call.
    """
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
                          AND (%(by_conv)s OR m.created_at >= COALESCE(NULLIF(%(since)s, '')::timestamptz, '-infinity')))
           AND (NOT %(by_conv)s OR c.started_at >= COALESCE(NULLIF(%(since)s, '')::timestamptz, '-infinity'))
         ORDER BY CASE WHEN %(priority)s = '' THEN false
                       WHEN %(by_conv)s THEN c.started_at >= NULLIF(%(priority)s, '')::timestamptz
                       ELSE EXISTS (SELECT 1 FROM messages m
                                     WHERE m.conversation_id = c.id AND m.author IN ('martin', 'claude')
                                       AND m.sequence_num > COALESCE(p.last_seq, -1)
                                       AND m.created_at >= NULLIF(%(priority)s, '')::timestamptz) END DESC,
                  c.started_at, c.id
        """,
        {"idle": cfg.idle_minutes, "since": since_sql(cfg), "priority": priority_sql(cfg),
         "by_conv": cfg.since_scope == "conversation"},
    ).fetchall()


def pending_exchanges(conn, cfg, conv_id, last_seq):
    rows = conn.execute(
        """
        SELECT author, content, created_at, sequence_num FROM messages
         WHERE conversation_id = %(c)s AND author IN ('martin', 'claude') AND sequence_num > %(seq)s
           AND (%(by_conv)s OR created_at >= COALESCE(NULLIF(%(since)s, '')::timestamptz, '-infinity'))
         ORDER BY sequence_num
        """,
        {"c": conv_id, "seq": last_seq, "since": since_sql(cfg), "by_conv": cfg.since_scope == "conversation"},
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

def db_error_text(e):
    """Type and SQLSTATE only: a psycopg message can carry the DETAIL line, which quotes fact text."""
    return f"database: {type(e).__name__} (sqlstate {getattr(e, 'sqlstate', None)})"


def db_is_systemic(e):
    """A lost or unusable connection says nothing about the exchange; a constraint error does."""
    import psycopg

    return isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError))


def extract_pending(conn, cfg, extractor, stats, limit, dry_run, out=print):
    started = time.monotonic()
    dry_seen = {}
    streak = 0  # consecutive systemic errors
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
            error, systemic = res.error, res.systemic
            if not error and not dry_run:
                try:
                    commit_exchange(conn, ex, conv_id, host, extractor.version, res.facts)
                except Exception as e:  # noqa: BLE001 - the transaction rolled back
                    error, systemic = db_error_text(e), db_is_systemic(e)
            if error:
                stats.errors += 1
                log(f"conversation {conv_id} seq {ex.first_seq}-{ex.last_seq}: failed"
                    f"{' (systemic, not counted as an attempt)' if systemic else ''}: {error[:200]}")
                if systemic:
                    # An outage, a bad key, a rate limit: nothing to learn about this exchange, so no
                    # attempt is recorded. Several in a row mean stop and let the next tick try again.
                    stats.systemic_errors += 1
                    streak += 1
                    if streak >= cfg.max_systemic:
                        stats.aborted = True
                        log(f"{streak} systemic errors in a row; stopping this run")
                        return
                    break  # leave this conversation for the next run, try the next one
                streak = 0
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
            streak = 0
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


def embed_failure_is_systemic(e):
    """Cal unreachable or erroring (transport, 5xx) versus this one fact being unembeddable (4xx, bad shape)."""
    status = getattr(getattr(e, "response", None), "status_code", 0)  # httpx.HTTPStatusError
    return (is_systemic(e) or status >= 500
            or type(e).__name__ in ("ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"))


def embed_pending(conn, cfg, embed, stats):
    """Embed every unembedded fact, embed_batch at a time.

    All of them, not one batch: a fact without a vector is invisible to recall_facts' semantic
    search, and a busy run extracts more than one batch (29 Oct 2026: 269 facts against a
    batch of 200). A fact that fails is not tried again this run, so the loop always ends.
    """
    tried = []  # ids that failed this run; the next batch skips them
    streak = 0  # consecutive failures that point at Cal, not at the fact
    while True:
        rows = conn.execute(
            "SELECT id, text FROM facts WHERE embedding IS NULL AND deleted_at IS NULL AND NOT (id = ANY(%s)) "
            "ORDER BY created_at LIMIT %s",
            (tried, cfg.embed_batch)).fetchall()
        if not rows:
            return
        for fact_id, text in rows:
            try:
                vec = embed(text)
            except Exception as e:  # noqa: BLE001 - retried next run
                stats.embed_errors += 1
                tried.append(fact_id)
                log(f"fact {fact_id}: embedding failed: {type(e).__name__}")
                # A fact Cal rejects is skipped, so a few permanently bad ones cannot hold up the rest;
                # only an unreachable or failing Cal stops the run's embedding.
                streak = streak + 1 if embed_failure_is_systemic(e) else 0
                if streak >= cfg.max_systemic:
                    log(f"{streak} embedding failures in a row; leaving the rest for the next run")
                    return
                continue
            streak = 0
            conn.execute(
                "UPDATE facts SET embedding = %s::vector, embedder = %s, embedder_board = NULL, embedder_fw = NULL, "
                "updated_at = now() WHERE id = %s AND embedding IS NULL",
                ("[" + ",".join(repr(float(x)) for x in vec) + "]", cfg.embed_tag, fact_id))
            stats.embedded += 1


def run(conn, cfg, extractor, embed, limit=None, dry_run=False, out=print):
    """One service run over an open autocommit connection. Returns Stats; None if another run holds the lock.

    Raises ConfigError (before touching anything) when FACTS_SINCE is unusable. A dry run with no
    FACTS_SINCE looks at everything, as --limit bounds what it pays for.
    """
    if dry_run and not cfg.since:
        cfg = replace(cfg, since="all")
    since = since_sql(cfg)
    log(f"facts run: since {since or 'the beginning of history (FACTS_SINCE=all)'}{', dry run' if dry_run else ''}")
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
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        except Exception as e:  # noqa: BLE001 - must not mask the real error; the lock dies with the session
            log(f"could not release the advisory lock: {type(e).__name__}")
    stats.seconds = round(time.monotonic() - t, 1)
    return stats


def connect(cfg):
    """An autocommit connection with a statement timeout and TCP keepalives, so a dead Luke fails the run."""
    import psycopg

    return psycopg.connect(cfg.dsn, autocommit=True, connect_timeout=15,
                           options=f"-c statement_timeout={cfg.statement_timeout_ms}",
                           keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract facts from new conversation-memory exchanges.")
    ap.add_argument("--limit", type=int, help="process at most this many exchanges (default FACTS_MAX_EXCHANGES)")
    ap.add_argument("--dry-run", action="store_true",
                    help="call the model but write nothing to the database and embed nothing; print the facts")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if cfg.provider == "off":
        log("facts extraction is off (FACTS_PROVIDER=off)")
        return
    if not cfg.dsn:
        sys.exit("FACTS_DATABASE_URL (or CM_DATABASE_URL) is not set")
    if cfg.embedder not in EMBEDDERS:
        sys.exit(f"FACTS_EMBEDDER={cfg.embedder!r}: want one of {', '.join(EMBEDDERS)}")
    if cfg.embedder == "ollama" and not cfg.embed_url and not args.dry_run:
        sys.exit("FACTS_EMBED_URL (or OLLAMA_BASE_URL) is not set")
    try:
        if not args.dry_run:
            since_sql(cfg)  # fail before paying for anything
        extractor = make_extractor(cfg)
        if cfg.embedder == "hook":
            cfg, embed = make_hook_embedder(cfg)
        else:
            embed = make_embedder(cfg) if cfg.embed_url else None
        with connect(cfg) as conn:
            stats = run(conn, cfg, extractor, embed, args.limit, args.dry_run)
    except ConfigError as e:
        sys.exit(str(e))
    if stats is None:
        return
    print("facts-run " + json.dumps(stats.__dict__, sort_keys=True), flush=True)
    if stats.failed:
        # Errors and nothing done: exit non-zero so the oneshot unit fails and the failed-unit alert fires.
        sys.exit(1)


if __name__ == "__main__":
    main()
