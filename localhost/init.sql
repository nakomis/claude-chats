-- Schema for the single-machine edition. Kept identical to the distributed
-- edition's ../init.sql so a database can be moved between the two, and so a
-- backup taken under one restores under the other.
--
-- The `host` column is always the same value here, but it stays because the
-- distributed edition uses it to tell several machines' conversations apart
-- and the two schemas must not diverge.
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
