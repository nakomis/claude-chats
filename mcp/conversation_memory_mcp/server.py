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
) -> list[dict]:
    """Semantically search previous Claude Code conversations.

    Returns the most relevant messages plus a few lines of surrounding context
    so you can understand what was being discussed.  Useful for recovering
    context lost to compaction, or for finding how a problem was solved before.

    Args:
        query:        Natural-language description of what you are looking for.
        limit:        Number of results to return (default 5).
        project_path: If supplied, restrict results to that project directory.
    """
    vec = _vec(get_embedding(query, for_query=True))

    with _conn() as conn:
        with conn.cursor() as cur:
            base = """
                SELECT m.id, m.conversation_id, c.session_id, c.project_path,
                       m.role, m.content, m.sequence_num, m.created_at,
                       1 - (m.embedding <=> %s::vector) AS similarity
                FROM   messages m
                JOIN   conversations c ON c.id = m.conversation_id
                WHERE  m.embedding IS NOT NULL
            """
            params: list = [vec]

            if project_path:
                base += " AND c.project_path = %s"
                params.append(project_path)

            base += " ORDER BY m.embedding <=> %s::vector LIMIT %s"
            params += [vec, limit]

            cur.execute(base, params)
            rows = cur.fetchall()

            results = []
            for msg_id, conv_id, session_id, proj, role, content, seq, ts, sim in rows:
                # Fetch surrounding messages for context
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
                    "similarity":   round(float(sim), 4),
                    "context":      context,
                })

            return results


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
