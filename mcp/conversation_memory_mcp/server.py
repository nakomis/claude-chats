"""
Conversation Memory MCP server.

Provides semantic search and retrieval over Claude Code conversations that
have been recorded by the Stop hook.

Embedding provider is configured via CLAUDE_CHATS_PROVIDER — see embed.py.
"""

import os

import psycopg
from mcp.server.fastmcp import FastMCP

from conversation_memory_mcp.embed import get_embedding

DB_URL = os.environ.get("CLAUDE_CHATS_DB_URL", "postgresql://claude:claude@localhost:5433/claude_chats")

CONTEXT_WINDOW = 2  # messages either side of a search hit

mcp = FastMCP("conversation-memory")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


def _conn():
    return psycopg.connect(DB_URL)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_memory(
    query: str,
    limit: int = 5,
    project_path: str | None = None,
    mode: str = "hybrid",
) -> dict:
    """Search previous Claude Code conversations.

    Returns the most relevant messages plus surrounding context.  Useful for
    recovering context lost to compaction, or finding how a problem was solved.

    Return value shape:
        {
          "status":   "ok" | "fallback" | "no_results",
          "mode_used": "hybrid" | "semantic" | "fulltext",
          "warning":  "<string>",   # only present when status != "ok"
          "results":  [...]
        }

    Each result contains: session_id, project_path, role, sequence_num,
    created_at, score, context (surrounding messages).

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

    Args:
        query:        Search string.  For "fulltext"/"hybrid", supports websearch
                      syntax (quote phrases, OR, -exclude).  For "semantic",
                      use a HyDE sentence for best results.
        limit:        Number of results to return (default 5).
        project_path: If supplied, restrict results to that project directory.
        mode:         Search mode: "hybrid" (default), "semantic", or "fulltext".
    """
    project_filter = "AND c.project_path = %s" if project_path else ""
    candidates = limit * 10

    warning: str | None = None
    mode_used = mode
    vec: str | None = None

    if mode in ("hybrid", "semantic"):
        try:
            vec = _vec(get_embedding(query, for_query=True))
        except Exception:
            if mode == "hybrid":
                mode_used = "fulltext"
                warning = "Embedding service unavailable; fell back to full-text search."
            else:
                return {
                    "status": "fallback",
                    "mode_used": "none",
                    "warning": "Embedding service unavailable and mode='semantic' — no results.",
                    "results": [],
                }

    with _conn() as conn:
        with conn.cursor() as cur:
            if mode_used == "semantic":
                rows = _query_semantic(cur, vec, project_path, project_filter, limit)
            elif mode_used == "fulltext":
                rows = _query_fulltext(cur, query, project_path, project_filter, limit)
            else:
                rows = _query_hybrid(cur, vec, query, project_path, project_filter,
                                     limit, candidates)

            results = []
            for msg_id, conv_id, session_id, proj, role, content, seq, ts, score in rows:
                cur.execute(
                    """
                    SELECT role, content, sequence_num
                    FROM   messages
                    WHERE  conversation_id = %s
                      AND  sequence_num BETWEEN %s AND %s
                    ORDER  BY sequence_num
                    """,
                    (conv_id, max(0, seq - CONTEXT_WINDOW), seq + CONTEXT_WINDOW),
                )
                context = [
                    {
                        "role": r,
                        "seq":  s,
                        "content": c if s == seq else (c[:300] + "…" if len(c) > 300 else c),
                        "is_match": s == seq,
                    }
                    for r, c, s in cur.fetchall()
                ]
                results.append({
                    "session_id":   session_id,
                    "project_path": proj,
                    "role":         role,
                    "sequence_num": seq,
                    "created_at":   str(ts),
                    "score":        round(float(score), 6),
                    "context":      context,
                })

    out: dict = {
        "status": "ok" if not warning else "fallback",
        "mode_used": mode_used,
        "results": results,
    }
    if warning:
        out["warning"] = warning
    return out


def _query_semantic(cur, vec: str, project_path, project_filter: str, limit: int):
    sql = f"""
        SELECT m.id, m.conversation_id, c.session_id, c.project_path,
               m.role, m.content, m.sequence_num, m.created_at,
               1 - (m.embedding <=> %s::vector) AS score
        FROM   messages m
        JOIN   conversations c ON c.id = m.conversation_id
        WHERE  m.embedding IS NOT NULL
        {project_filter}
        ORDER  BY m.embedding <=> %s::vector
        LIMIT  %s
    """
    params = [vec, *(([project_path]) if project_path else []), vec, limit]
    cur.execute(sql, params)
    return cur.fetchall()


def _query_fulltext(cur, query: str, project_path, project_filter: str, limit: int):
    sql = f"""
        SELECT m.id, m.conversation_id, c.session_id, c.project_path,
               m.role, m.content, m.sequence_num, m.created_at,
               ts_rank_cd(m.content_tsv, q) AS score
        FROM   messages m
        JOIN   conversations c ON c.id = m.conversation_id,
               websearch_to_tsquery('english', %s) AS q
        WHERE  m.content_tsv @@ q
        {project_filter}
        ORDER  BY score DESC
        LIMIT  %s
    """
    params = [query, *(([project_path]) if project_path else []), limit]
    cur.execute(sql, params)
    return cur.fetchall()


def _query_hybrid(cur, vec: str, query: str, project_path, project_filter: str,
                  limit: int, candidates: int):
    sql = f"""
        WITH semantic AS (
            SELECT m.id,
                   ROW_NUMBER() OVER (ORDER BY m.embedding <=> %s::vector) AS rank
            FROM   messages m
            JOIN   conversations c ON c.id = m.conversation_id
            WHERE  m.embedding IS NOT NULL
            {project_filter}
            ORDER  BY m.embedding <=> %s::vector
            LIMIT  %s
        ),
        fulltext AS (
            SELECT m.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(m.content_tsv, q) DESC) AS rank
            FROM   messages m
            JOIN   conversations c ON c.id = m.conversation_id,
                   websearch_to_tsquery('english', %s) AS q
            WHERE  m.content_tsv @@ q
            {project_filter}
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
               m.role, m.content, m.sequence_num, m.created_at, r.score
        FROM   rrf r
        JOIN   messages m ON m.id = r.id
        JOIN   conversations c ON c.id = m.conversation_id
        ORDER  BY r.score DESC
        LIMIT  %s
    """
    proj = ([project_path] if project_path else [])
    params = [vec, *proj, vec, candidates, query, *proj, candidates, limit]
    cur.execute(sql, params)
    return cur.fetchall()


@mcp.tool()
def get_conversation(
    session_id: str,
    start_seq: int | None = None,
    end_seq: int | None = None,
) -> dict:
    """Retrieve messages from a past conversation by session ID.

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
                "SELECT id, project_path, git_branch, started_at, name FROM conversations WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"No conversation found for session_id '{session_id}'"}

            conv_id, project_path, git_branch, started_at, name = row

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

            sql += " ORDER BY sequence_num"
            cur.execute(sql, params)

            messages = [
                {"role": r, "seq": s, "created_at": str(t), "content": c}
                for r, c, s, t in cur.fetchall()
            ]

            return {
                "session_id":    session_id,
                "name":          name,
                "project_path":  project_path,
                "git_branch":    git_branch,
                "started_at":    str(started_at),
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

    Args:
        limit:        Maximum number of sessions to return (default 10).
        project_path: If supplied, restrict to that project directory.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT   c.session_id, c.project_path, c.git_branch, c.started_at,
                         c.name,
                         COUNT(m.id)       AS message_count,
                         MAX(m.created_at) AS last_message_at
                FROM     conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
            """
            params: list = []

            if project_path:
                sql += " WHERE c.project_path = %s"
                params.append(project_path)

            sql += " GROUP BY c.session_id, c.project_path, c.git_branch, c.started_at, c.name"
            sql += " ORDER BY c.started_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(sql, params)

            return [
                {
                    "session_id":     sid,
                    "name":           name,
                    "project_path":   pp,
                    "git_branch":     gb,
                    "started_at":     str(sa),
                    "message_count":  mc,
                    "last_message_at": str(lm) if lm else None,
                }
                for sid, pp, gb, sa, name, mc, lm in cur.fetchall()
            ]


# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
