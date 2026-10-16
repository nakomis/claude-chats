"""
Conversation Memory MCP server.

Provides semantic search and retrieval over Claude Code conversations that
have been recorded by the Stop hook.

Embedding provider is configured via CLAUDE_CHATS_PROVIDER — see embed.py.
"""

import calendar
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from mcp.server.fastmcp import FastMCP

from conversation_memory_mcp.embed import get_embedding

DB_URL = os.environ.get("CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats")

CONTEXT_WINDOW = 2  # messages either side of a search hit

# How far back search_memory looks when the caller says nothing (CHATS-14).
# The corpus grows without bound and recent conversations are what sessions
# almost always want; a caller after older material asks for it explicitly.
DEFAULT_SINCE = "3 months"

# pgvector's iterative index scans (0.8+). Not optional once `since` exists:
# with ivfflat the ANN scan chooses candidates by distance and the created_at
# predicate is applied *afterwards*, so a date window silently throws away most
# of the k results — ask for 10, get 2, or none at all, with no error. An
# iterative scan keeps pulling candidates until the filter is satisfied.
# Measured on Luke (118,283 messages, 65,401 inside 3 months), limit=10:
# unfiltered 10 rows; 3-month filter with iterative_scan=off 0 rows; same
# filter with relaxed_order 10 rows.
#
# "relaxed_order" is the only iterative mode ivfflat offers — strict_order is
# HNSW-only, and ivfflat rejects it (at query time, with a WARNING, having
# accepted the SET). Relaxed ordering is right for this search anyway: the
# results are fused by RRF and re-ranked, so a slightly out-of-order candidate
# stream costs nothing.
ITERATIVE_SCAN = "relaxed_order"

# How many of the index's lists an ivfflat scan visits. pgvector's default of 1
# misses far too much: measured on Luke (123,481 rows, lists=100, 26 queries,
# recall against an exact scan), probes=1 returned 0.58 of the true top 10 and
# some queries none of them. probes=20 returned 0.97 (0.99 inside 3 months) at
# p50 0.23 s, max 0.45 s; an exact scan takes 2-7 s (CHATS-15).
#
# CHATS-14 measured probes=5 at 8.5 s and 23.5 s and left this at 1. That was
# not the index being slow: at probes=5 the planner's cost estimate tips it
# onto a parallel seq scan + top-N sort, while at 4 and at 10 it stays on the
# index. The estimate is not monotonic in probes, so any new value needs an
# EXPLAIN check first; scripts/ann-recall-bench.py measures recall, latency and
# the plan per setting.
_PROBES_MAX = 32768  # pgvector's own upper bound


def _probes_from_env(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        return 20
    try:
        probes = int(raw)
    except ValueError:
        raise ValueError(f"CLAUDE_CHATS_IVFFLAT_PROBES must be an integer, got {raw!r}") from None
    if not 1 <= probes <= _PROBES_MAX:
        raise ValueError(f"CLAUDE_CHATS_IVFFLAT_PROBES must be 1-{_PROBES_MAX}, got {probes}")
    return probes


IVFFLAT_PROBES = _probes_from_env(os.environ.get("CLAUDE_CHATS_IVFFLAT_PROBES"))

# Postgres stores timestamptz in UTC (correct — leave it). This is purely how
# the MCP *presents* it: a bare UTC clock time (21:37) invites a reader whose
# wall clock says 22:37 BST to misread it as an hour old — which is exactly what
# happened the first time the system was queried across sessions (HOME-199).
LOCAL_TZ = ZoneInfo(os.environ.get("CLAUDE_CHATS_TZ", "Europe/London"))

mcp = FastMCP("conversation-memory")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


def _conn():
    return psycopg.connect(DB_URL)


def _aware_utc(ts: datetime) -> datetime:
    """Treat a naive datetime as UTC; leave an aware one alone."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _relative(ts: datetime, now: datetime | None = None) -> str:
    """A UTC-correct 'N minutes ago' — computed against real elapsed time, so it
    can't be misread whatever timezone the reader is in."""
    ts = _aware_utc(ts)
    now = now or datetime.now(timezone.utc)
    secs = (now - ts).total_seconds()
    future, s = secs < 0, abs(secs)
    if s < 45:
        return "just now"
    mins = s / 60
    if mins < 60:
        v, unit = round(mins), "minute"
    elif mins < 60 * 24:
        v, unit = round(mins / 60), "hour"
    elif mins < 60 * 24 * 7:
        v, unit = round(mins / 60 / 24), "day"
    else:
        v, unit = round(mins / 60 / 24 / 7), "week"
    label = f"{v} {unit}{'' if v == 1 else 's'}"
    return f"in {label}" if future else f"{label} ago"


def _human_time(ts: datetime, now: datetime | None = None) -> str:
    """Present a timestamp so it cannot be misread: lead with the relative form,
    then the operator-local wall-clock time with its zone, e.g.
    '2 minutes ago (2026-07-24 22:37 BST)'. The exact UTC value stays available
    as a sibling `*_utc` field for correlation (see _utc_iso)."""
    local = _aware_utc(ts).astimezone(LOCAL_TZ)
    return f"{_relative(ts, now)} ({local.strftime('%Y-%m-%d %H:%M %Z')})"


def _utc_iso(ts: datetime) -> str:
    """The raw UTC value, ISO 8601 — for exact correlation across machines."""
    return _aware_utc(ts).astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The `since` window (CHATS-14)
# ---------------------------------------------------------------------------

# "all" and friends mean no lower bound at all — search the whole corpus.
_UNBOUNDED = {"", "all", "any", "ever", "none", "null", "forever", "unlimited",
              "no limit", "nolimit", "beginning", "start"}

_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds",
    "second": "seconds", "seconds": "seconds",
    "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks",
    "mo": "months", "mon": "months", "mons": "months",
    "month": "months", "months": "months",
    "y": "years", "yr": "years", "yrs": "years", "year": "years", "years": "years",
}

# "3 months", "30d", "last 2 weeks", "6 hours ago", "1 year"
_PERIOD = re.compile(
    r"^(?:last|past|previous)?\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*(?:ago)?$"
)


def _minus_months(ts: datetime, months: int) -> datetime:
    """Calendar-correct month subtraction, clamping the day (31 Mar - 1 month
    is 28/29 Feb, not an invalid date)."""
    total = ts.year * 12 + (ts.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return ts.replace(year=year, month=month,
                      day=min(ts.day, calendar.monthrange(year, month)[1]))


def parse_since(value: str | None, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Resolve the `since` argument to (cutoff, label).

    A cutoff of None means no lower bound — the full history.

    Accepts a relative period ("3 months", "30d", "last 2 weeks"), an ISO date
    or timestamp ("2026-01-15", "2026-01-15T09:30:00Z"), or one of "all" /
    "none" / "ever" for no limit. None (argument omitted) means the default
    window, DEFAULT_SINCE.

    A bare "m" is rejected deliberately: it reads as minutes to half the world
    and months to the other half, and the two differ by a factor of 43,800.
    Say "min" or "mo".

    Raises ValueError, with the accepted forms in the message, on anything else.
    """
    now = now or datetime.now(timezone.utc)
    raw = DEFAULT_SINCE if value is None else str(value)
    text = raw.strip().lower()

    if text in _UNBOUNDED:
        return None, "no limit (full history)"

    m = _PERIOD.match(text)
    if m:
        qty_text, unit_text = m.group(1), m.group(2)
        if unit_text == "m":
            raise ValueError(
                "since='...m' is ambiguous — 'min' for minutes, 'mo' for months."
            )
        unit = _UNITS.get(unit_text)
        if unit:
            qty = float(qty_text)
            if unit in ("months", "years"):
                if qty != int(qty):
                    raise ValueError(
                        f"since={raw!r}: whole {unit} only — use days or weeks "
                        "for a finer window."
                    )
                months = int(qty) * (12 if unit == "years" else 1)
                cutoff = _minus_months(now, months)
            else:
                cutoff = now - timedelta(**{unit: qty})
            return cutoff, raw.strip()

    # An absolute date or timestamp. Naive values are read in the operator's
    # local zone — someone typing "2026-01-15" means their own midnight.
    try:
        ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"since={raw!r} not understood. Use a period (\"3 months\", \"30d\", "
            "\"last 2 weeks\"), an ISO date (\"2026-01-15\") or timestamp, or "
            "\"all\" for the full history."
        ) from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=LOCAL_TZ)
    return ts, ts.isoformat()


def _enable_iterative_scan(cur) -> str | None:
    """Turn pgvector's iterative index scans on for this session and return the
    value genuinely in effect, or None if it could not be enabled.

    The order of these statements is the whole point. Postgres treats a dotted
    GUC whose module has not yet been loaded as a free-text placeholder, so in a
    fresh session `SET ivfflat.iterative_scan = banana` *succeeds* and SHOW
    cheerfully echoes "banana" while the scan runs with the setting ignored —
    set and forgotten, no symptom. Touching the vector type first loads
    pgvector's own GUC definitions; after that an invalid value is rejected at
    SET time and the read-back is authoritative. Both verified against Luke's
    pgvector 0.8.5 (CHATS-14).
    """
    try:
        # Loads pgvector's GUCs. Dimensions are irrelevant — any vector
        # operator will do.
        cur.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
        # A GUC name cannot be parameterised; the value is a module constant,
        # never caller input.
        cur.execute(f"SET ivfflat.iterative_scan = {ITERATIVE_SCAN}")
        cur.execute("SHOW ivfflat.iterative_scan")
        return cur.fetchone()[0]
    except psycopg.Error:
        # A rejected value or an unknown GUC aborts the transaction; wind it
        # back so the search itself can still run (unfiltered it is unaffected).
        cur.connection.rollback()
        return None


def _set_probes(cur) -> int | None:
    """Set ivfflat.probes for this session and return the value genuinely in
    effect, or None if it could not be set.

    Call after `_enable_iterative_scan`, which loads pgvector's GUCs (without
    that, the SET lands on a placeholder and the read-back proves nothing). A
    failure here rolls the transaction back, which also undoes the iterative
    scan; with IVFFLAT_PROBES range-checked at startup that needs a server
    without pgvector at all, where the iterative scan has already failed.
    """
    try:
        # Validated int from startup, never caller input.
        cur.execute(f"SET ivfflat.probes = {IVFFLAT_PROBES}")
        cur.execute("SHOW ivfflat.probes")
        return int(cur.fetchone()[0])
    except (psycopg.Error, ValueError):
        cur.connection.rollback()
        return None


def _filters(project_path: str | None, since: datetime | None) -> tuple[str, list]:
    """The WHERE additions shared by all three search paths, as a SQL fragment
    plus its parameters in order. One place, so the semantic and full-text
    halves of a hybrid search cannot drift apart."""
    frag, params = "", []
    if project_path:
        frag += "\n        AND c.project_path = %s"
        params.append(project_path)
    if since is not None:
        frag += "\n        AND m.created_at >= %s"
        params.append(since)
    return frag, params


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_memory(
    query: str,
    limit: int = 5,
    project_path: str | None = None,
    mode: str = "hybrid",
    since: str | None = DEFAULT_SINCE,
) -> dict:
    """Search previous Claude Code conversations.

    Returns the most relevant messages plus surrounding context.  Useful for
    recovering context lost to compaction, or finding how a problem was solved.

    By default only the last 3 months are searched — see `since`, which is the
    "from this date onwards" bound.  Widen it with since="all" when the answer
    may be older, or when a search comes back empty.

    Return value shape:
        {
          "status":   "ok" | "fallback" | "no_results" | "error",
          "mode_used": "hybrid" | "semantic" | "fulltext",
          "since":    "3 months",            # the window actually applied
          "since_utc": "2026-07-15T…+00:00", # its cutoff, null for no limit
          "warning":  "<string>",   # only present when status != "ok"
          "results":  [...]
        }

    Each result contains: session_id, name, ai_title, project_path, role,
    sequence_num,
    created_at (relative + operator-local, e.g. "2 minutes ago (2026-07-24
    22:37 BST)"), created_at_utc (raw ISO-8601 UTC for exact correlation),
    score, context (surrounding messages).

    Session titles — two, never merged:
        name      What Martin called the session with /rename. Authoritative;
                  null if it was never renamed.
        ai_title  Claude Code's own generated title. Present for almost every
                  session, but a machine guess. Use it to identify a session
                  when name is null; don't present it as Martin's name.

    Modes:
        "hybrid" (default)
            Combines vector similarity with full-text search using Reciprocal
            Rank Fusion.  Best overall — works for both fuzzy semantic recall
            and exact phrase/keyword lookups.  Falls back to "fulltext" if the
            embedding service is unavailable (check "status" in the response).

        "semantic"
            Vector-only search.  Use when describing a concept rather than
            exact words, e.g. "the conversation about umbrella insurance".
            Apply HyDE for best results: phrase the query as a short sentence
            that *would appear* in the target conversation.
            Returns an error result (no fallback) if embeddings are unavailable.

        "fulltext"
            PostgreSQL tsvector/tsquery only — no embedding call.  Use for
            known exact phrases, function names, error messages, or quoted
            strings.  Also reliable when Ollama is offline.
            Supports websearch syntax: quote phrases, use OR, prefix - to exclude.

    HyDE guidance (relevant for "semantic" and the semantic half of "hybrid"):
        Bare keywords score poorly against conversational text.  Write the
        query as a short hypothetical sentence that *would appear* in the
        conversation, e.g. "It's raining, should I bring an umbrella?" rather
        than "umbrella".  When a first search misses, ask Martin for any
        extra keywords or context before retrying — do not guess.

    Fallback when hybrid misses:
        If hybrid search returns weak results for something Martin confirms
        exists, retry with mode="fulltext" and a verbatim distinctive phrase
        from that conversation (e.g. a specific error message, command, or
        sentence).  Hybrid's semantic half fails when the query framing differs
        from the actual wording; fulltext hits exact phrases regardless.
        If that still misses, the material may simply be older than the default
        3-month window — retry with since="all".

    Args:
        query:        Search string.  For "fulltext"/"hybrid", supports websearch
                      syntax (quote phrases, OR, -exclude).  For "semantic",
                      use a HyDE sentence for best results.
        limit:        Number of results to return (default 5).
        project_path: If supplied, restrict results to that project directory.
        mode:         Search mode: "hybrid" (default), "semantic", or "fulltext".
        since:        How far back to search — the earliest date to search from.
                      Default "3 months".  Accepts a relative period
                      ("3 months", "30d", "6 weeks", "1 year"), an ISO date or
                      timestamp ("2026-01-15"), or "all" for the full history.
                      Use "all" when the conversation may be older, or when a
                      search inside the default window comes back empty.
                      ("m" is rejected as ambiguous: say "min" or "mo".)
    """
    try:
        cutoff, since_label = parse_since(since)
    except ValueError as exc:
        return {
            "status": "error",
            "mode_used": "none",
            "since": str(since),
            "since_utc": None,
            "warning": str(exc),
            "results": [],
        }

    where, where_params = _filters(project_path, cutoff)
    candidates = limit * 10

    warnings: list[str] = []
    mode_used = mode
    vec: str | None = None

    if mode in ("hybrid", "semantic"):
        try:
            vec = _vec(get_embedding(query, for_query=True))
        except Exception:
            if mode == "hybrid":
                mode_used = "fulltext"
                warnings.append("Embedding service unavailable; fell back to full-text search.")
            else:
                return {
                    "status": "fallback",
                    "mode_used": "none",
                    "since": since_label,
                    "since_utc": _utc_iso(cutoff) if cutoff else None,
                    "warning": "Embedding service unavailable and mode='semantic' — no results.",
                    "results": [],
                }

    with _conn() as conn:
        with conn.cursor() as cur:
            if mode_used in ("semantic", "hybrid"):
                scan = _enable_iterative_scan(cur)
                probes = _set_probes(cur)
                if probes is None:
                    scan = None  # its rollback undid the iterative scan too
                if cutoff is not None and scan != ITERATIVE_SCAN:
                    warnings.append(
                        "pgvector iterative index scans are unavailable "
                        f"(ivfflat.iterative_scan = {scan!r}); with a `since` window the "
                        "vector half of this search can return fewer results than asked "
                        "for. Use since='all', or mode='fulltext'."
                    )
                if probes != IVFFLAT_PROBES:
                    in_effect = ("could not be set, so pgvector's default of 1 applies"
                                 if probes is None else f"is {probes}, not {IVFFLAT_PROBES}")
                    warnings.append(
                        f"ivfflat.probes {in_effect}; the vector half of this search "
                        "will miss close matches (about 4 in 10 at probes=1). "
                        "mode='fulltext' is unaffected."
                    )

            if mode_used == "semantic":
                rows = _query_semantic(cur, vec, where, where_params, limit)
            elif mode_used == "fulltext":
                rows = _query_fulltext(cur, query, where, where_params, limit)
            else:
                rows = _query_hybrid(cur, vec, query, where, where_params,
                                     limit, candidates)

            results = []
            for msg_id, conv_id, session_id, proj, name, ai_title, role, content, seq, ts, score in rows:
                cur.execute(
                    """
                    SELECT id, role, content, sequence_num
                    FROM   messages
                    WHERE  conversation_id = %s
                      AND  sequence_num BETWEEN %s AND %s
                    ORDER  BY sequence_num, created_at
                    """,
                    (conv_id, max(0, seq - CONTEXT_WINDOW), seq + CONTEXT_WINDOW),
                )
                context = [
                    {
                        "role": r,
                        "seq":  s,
                        "content": c if i == msg_id else (c[:300] + "…" if len(c) > 300 else c),
                        # By id, not seq: a message sent mid-turn shares its
                        # predecessor's sequence_num (HOME-411).
                        "is_match": i == msg_id,
                    }
                    for i, r, c, s in cur.fetchall()
                ]
                results.append({
                    "session_id":   session_id,
                    "name":         name,
                    "ai_title":     ai_title,
                    "project_path": proj,
                    "role":         role,
                    "sequence_num": seq,
                    "created_at":   _human_time(ts),
                    "created_at_utc": _utc_iso(ts),
                    "score":        round(float(score), 6),
                    "context":      context,
                })

    status = "ok"
    if not results:
        status = "no_results"
        if cutoff is not None:
            warnings.append(
                f"Nothing found in the search window (since={since_label!r}, i.e. "
                f"{_human_time(cutoff)} onwards). Retry with since='all' to search "
                "the full history."
            )
    elif warnings:
        status = "fallback"

    out: dict = {
        "status": status,
        "mode_used": mode_used,
        "since": since_label,
        "since_utc": _utc_iso(cutoff) if cutoff else None,
        "results": results,
    }
    if warnings:
        out["warning"] = " ".join(warnings)
    return out


def _query_semantic(cur, vec: str, where: str, where_params: list, limit: int):
    sql = f"""
        SELECT m.id, m.conversation_id, c.session_id, c.project_path,
               c.name, c.ai_title, m.role, m.content, m.sequence_num, m.created_at,
               1 - (m.embedding <=> %s::vector) AS score
        FROM   messages m
        JOIN   conversations c ON c.id = m.conversation_id
        WHERE  m.embedding IS NOT NULL{where}
        ORDER  BY m.embedding <=> %s::vector
        LIMIT  %s
    """
    cur.execute(sql, [vec, *where_params, vec, limit])
    # relaxed_order lets the iterative scan hand rows back slightly out of
    # distance order; restore it (score is the last column).
    return sorted(cur.fetchall(), key=lambda r: r[-1], reverse=True)


def _query_fulltext(cur, query: str, where: str, where_params: list, limit: int):
    sql = f"""
        SELECT m.id, m.conversation_id, c.session_id, c.project_path,
               c.name, c.ai_title, m.role, m.content, m.sequence_num, m.created_at,
               ts_rank_cd(m.content_tsv, q) AS score
        FROM   messages m
        JOIN   conversations c ON c.id = m.conversation_id,
               websearch_to_tsquery('english', %s) AS q
        WHERE  m.content_tsv @@ q{where}
        ORDER  BY score DESC
        LIMIT  %s
    """
    cur.execute(sql, [query, *where_params, limit])
    return cur.fetchall()


def _query_hybrid(cur, vec: str, query: str, where: str, where_params: list,
                  limit: int, candidates: int):
    sql = f"""
        WITH semantic AS (
            SELECT m.id,
                   ROW_NUMBER() OVER (ORDER BY m.embedding <=> %s::vector) AS rank
            FROM   messages m
            JOIN   conversations c ON c.id = m.conversation_id
            WHERE  m.embedding IS NOT NULL{where}
            ORDER  BY m.embedding <=> %s::vector
            LIMIT  %s
        ),
        fulltext AS (
            SELECT m.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(m.content_tsv, q) DESC) AS rank
            FROM   messages m
            JOIN   conversations c ON c.id = m.conversation_id,
                   websearch_to_tsquery('english', %s) AS q
            WHERE  m.content_tsv @@ q{where}
            ORDER  BY ts_rank_cd(m.content_tsv, q) DESC
            LIMIT  %s
        ),
        rrf AS (
            SELECT COALESCE(s.id, f.id) AS id,
                   COALESCE(1.0 / (60 + s.rank), 0) +
                   COALESCE(1.0 / (60 + f.rank), 0) AS score
            FROM   semantic s
            FULL OUTER JOIN fulltext f ON s.id = f.id
        )
        SELECT m.id, m.conversation_id, c.session_id, c.project_path,
               c.name, c.ai_title, m.role, m.content, m.sequence_num, m.created_at, r.score
        FROM   rrf r
        JOIN   messages m ON m.id = r.id
        JOIN   conversations c ON c.id = m.conversation_id
        ORDER  BY r.score DESC
        LIMIT  %s
    """
    params = [vec, *where_params, vec, candidates,
              query, *where_params, candidates, limit]
    cur.execute(sql, params)
    return cur.fetchall()


@mcp.tool()
def get_conversation(
    session_id: str,
    start_seq: int | None = None,
    end_seq: int | None = None,
) -> dict:
    """Retrieve messages from a past conversation by session ID.

    Returns name (Martin's /rename, or null) and ai_title (Claude Code's
    generated title) separately — see search_memory.

    For very long conversations, use start_seq / end_seq to page through the
    transcript (sequence numbers are shown in search_memory results).
    Omit both to retrieve the full conversation.

    Args:
        session_id: The session ID (from search_memory or list_recent_sessions).
        start_seq:  First sequence number to return (inclusive).
        end_seq:    Last sequence number to return (inclusive).
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, project_path, git_branch, started_at, name, ai_title "
                "FROM conversations WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"No conversation found for session_id '{session_id}'"}

            conv_id, project_path, git_branch, started_at, name, ai_title = row

            cur.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = %s",
                (conv_id,),
            )
            total = cur.fetchone()[0]

            sql = """
                SELECT role, content, sequence_num, created_at
                FROM   messages
                WHERE  conversation_id = %s
            """
            params: list = [conv_id]

            if start_seq is not None:
                sql += " AND sequence_num >= %s"
                params.append(start_seq)
            if end_seq is not None:
                sql += " AND sequence_num <= %s"
                params.append(end_seq)

            # created_at breaks ties: a message sent mid-turn shares its
            # predecessor's sequence_num (HOME-411).
            sql += " ORDER BY sequence_num, created_at"
            cur.execute(sql, params)

            messages = [
                {"role": r, "seq": s, "created_at": _human_time(t),
                 "created_at_utc": _utc_iso(t), "content": c}
                for r, c, s, t in cur.fetchall()
            ]

            return {
                "session_id":    session_id,
                "name":          name,
                "ai_title":      ai_title,
                "project_path":  project_path,
                "git_branch":    git_branch,
                "started_at":    _human_time(started_at),
                "started_at_utc": _utc_iso(started_at),
                "total_messages": total,
                "returned":      len(messages),
                "messages":      messages,
            }


@mcp.tool()
def list_recent_sessions(
    limit: int = 10,
    project_path: str | None = None,
) -> list[dict]:
    """List recent conversation sessions, newest first.

    Each session has name (Martin's /rename, or null) and ai_title (Claude
    Code's generated title) — separate fields; see search_memory.

    Args:
        limit:        Maximum number of sessions to return (default 10).
        project_path: If supplied, restrict to that project directory.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT   c.session_id, c.project_path, c.git_branch, c.started_at,
                         c.name, c.ai_title,
                         COUNT(m.id)       AS message_count,
                         MAX(m.created_at) AS last_message_at
                FROM     conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
            """
            params: list = []

            if project_path:
                sql += " WHERE c.project_path = %s"
                params.append(project_path)

            sql += " GROUP BY c.session_id, c.project_path, c.git_branch, c.started_at, c.name, c.ai_title"
            sql += " ORDER BY c.started_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)

            return [
                {
                    "session_id":     sid,
                    "name":           name,
                    "ai_title":       ai_title,
                    "project_path":   pp,
                    "git_branch":     gb,
                    "started_at":     _human_time(sa),
                    "message_count":  mc,
                    "last_message_at": _human_time(lm) if lm else None,
                }
                for sid, pp, gb, sa, name, ai_title, mc, lm in cur.fetchall()
            ]


# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
