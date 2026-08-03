-- Source of truth for the conversation-memory pg schema. Vendored into
-- home-infra at luke/deployment/claude-chats/init.sql, which is what runs on
-- Luke. tests/test_pg_schema.py and that repo's check pin the same DDL checksum
-- so the copy cannot silently drift — change one and both go red (HOME-194).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS conversations (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT        NOT NULL UNIQUE,
    project_path TEXT       NOT NULL DEFAULT '',
    git_branch  TEXT        NOT NULL DEFAULT '',
    name        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotent upgrade: add columns introduced after initial release
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS name TEXT;

-- Which machine the conversation happened on. Several machines (desktop, work
-- laptop, a loaner while one is in for repair) share this database, so search
-- can span every tenant or filter to one. Nullable: rows from the
-- single-machine era predate the concept.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS host TEXT;

CREATE INDEX IF NOT EXISTS conversations_host_idx         ON conversations (host);
CREATE INDEX IF NOT EXISTS conversations_project_path_idx ON conversations (project_path);
CREATE INDEX IF NOT EXISTS conversations_started_at_idx   ON conversations (started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    message_uuid    TEXT        NOT NULL UNIQUE,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT        NOT NULL,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sequence_num    INTEGER     NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_conversation_id_idx ON messages (conversation_id);
CREATE INDEX IF NOT EXISTS messages_sequence_num_idx    ON messages (conversation_id, sequence_num);
-- ivfflat index for approximate nearest-neighbour search (cosine distance)
-- lists=100 is a reasonable default; tune upward as the table grows
CREATE INDEX IF NOT EXISTS messages_embedding_idx
    ON messages USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Who produced the message: 'martin', 'claude', 'tool', or 'unknown'
-- (HOME-310/311). `role` cannot express this — Claude Code delivers tool
-- results as role='user' messages, so before this column existed only 14% of
-- role='user' rows were actually typed by a person, and every semantic search
-- competed against thousands of embedded command outputs (HOME-309).
--
-- Backfilled rows keep 'unknown' rather than being guessed at: honest and
-- greppable, where defaulting to 'martin' would manufacture the exact bad data
-- this exists to remove. No CHECK constraint yet, deliberately — one would
-- have to be added NOT VALID and validated separately against 70k existing
-- rows, and there is nothing to constrain until the backfill lands (HOME-311).
ALTER TABLE messages ADD COLUMN IF NOT EXISTS author TEXT NOT NULL DEFAULT 'unknown';

-- Which tool produced a row where author='tool', resolved by the hook through
-- the tool_use_id. NULL for every other author, and for tool results whose
-- originating call was not in the transcript.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_name TEXT;

-- Model behind an author='claude' row. NULL otherwise. The corpus already
-- spans several models; being able to slice by one later is worth the column.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS model TEXT;

-- Every projection and most search paths filter on author, and the values are
-- few and skewed, so this earns its keep immediately.
CREATE INDEX IF NOT EXISTS messages_author_idx ON messages (author);
-- Partial: tool_name is NULL for ~70% of rows, so exclude them from the index.
CREATE INDEX IF NOT EXISTS messages_tool_name_idx
    ON messages (tool_name) WHERE tool_name IS NOT NULL;

-- Full-text search: generated tsvector column + GIN index
ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX IF NOT EXISTS messages_content_tsv_idx ON messages USING GIN (content_tsv);

-- Autovacuum is enabled globally by default, but the stock 10% analyze
-- threshold let the planner statistics drift badly out of date on the
-- insert-heavy messages table (row estimates were ~35x too low), which
-- degrades pgvector query plans. Tighten the per-table thresholds so
-- ANALYZE runs after ~2% churn and VACUUM after ~5%. Idempotent.
ALTER TABLE messages
    SET (autovacuum_analyze_scale_factor = 0.02,
         autovacuum_vacuum_scale_factor  = 0.05);
ALTER TABLE conversations
    SET (autovacuum_analyze_scale_factor = 0.02);
