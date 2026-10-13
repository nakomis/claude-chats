-- Source of truth for the conversation-memory pg schema. Vendored into
-- home-infra at luke/deployment/claude-chats/init.sql, which is what runs on
-- Luke. tests/test_pg_schema.py and that repo's check pin the same DDL checksum
-- so the copy cannot silently drift — change one and both go red (HOME-194).
-- Every statement here is re-run on each Luke playbook run, and several take
-- ACCESS EXCLUSIVE on `messages` even when they turn out to be no-ops (ADD
-- COLUMN IF NOT EXISTS does). Waiting for that lock queues every later query
-- on the table behind it, so a long reader (a backup's pg_dump) would stall
-- search for minutes. Give up after 5 s instead: the play fails loudly and
-- can be re-run (HOME-440 review).
SET lock_timeout = '5s';

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

-- Claude Code's own generated title for the session (HOME-391). Deliberately a
-- separate column from `name`: `name` is what the human chose with /rename,
-- this is a machine guess, and conflating the two would make a real name
-- indistinguishable from an invented one. Display with COALESCE(name, ai_title).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS ai_title TEXT;

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

-- Which embedder produced the row's vector (CHATS-5 / HOME-440). Kata, the
-- ESP32-S3 embedder, writes int8 vectors that agree with Ollama's to cos
-- ~0.9994: close, not identical, so a corpus mixing the two has to say which
-- is which for drift to be measurable. Values: 'ollama-mxbai-q8' (Cal) or
-- 'esp32s3-mxbai-int8[-vN]' (Kata; the suffix moves exactly when the kernels'
-- arithmetic does). NULL = embedded before tagging, which in practice was
-- Ollama on Cal or the Mac.
--
-- Board and firmware version are separate columns rather than one jsonb: the
-- shape is fixed and known, they are what drift analysis groups by, and plain
-- columns cannot quietly grow a misspelt key. NULL on Ollama rows (the tag
-- alone identifies those). All three follow the vector on a re-embed, so the
-- consumer overwrites them on conflict. Nullable with no default, so adding
-- them is a catalogue-only change, not a rewrite of the table.
-- Added in one statement, and only when missing (HOME-440 review): ALTER
-- TABLE takes ACCESS EXCLUSIVE even when every IF NOT EXISTS turns out to be
-- a no-op, and that queues behind any open reader and blocks every query
-- behind it. This file is reapplied on every Luke playbook run, so re-runs
-- check the catalogue first and take no lock at all; the first run is bound
-- by the lock_timeout at the top.
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'messages'
          AND column_name IN ('embedder', 'embedder_board', 'embedder_fw')) < 3 THEN
        ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS embedder TEXT,
            ADD COLUMN IF NOT EXISTS embedder_board TEXT,
            ADD COLUMN IF NOT EXISTS embedder_fw TEXT;
    END IF;
END
$$;

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
