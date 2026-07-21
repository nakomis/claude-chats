-- Bakeoff-only schema — NOT part of the live conversation-memory database.
--
-- The live schema is defined entirely in ../init.sql (conversations + messages,
-- with embeddings stored inline in messages.embedding). Retrieval only ever
-- reads messages.embedding.
--
-- These two tables exist purely to support the one-off embedding model
-- comparison in scripts/embed-bakeoff.py (and intrinsic-eval.py), which embeds
-- every message with several candidate models to compare retrieval quality.
-- They are intentionally kept out of init.sql so the live DB stays lean and
-- backups don't carry redundant, regenerable vectors.
--
-- Run this before (re-)running the bakeoff on a fresh database:
--   docker exec -i claude-chats-db psql -U claude -d claude_chats < scripts/bakeoff-schema.sql
--
-- Drop them again afterwards to reclaim the space:
--   DROP TABLE IF EXISTS message_embeddings, embedding_perf;

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per (message, candidate model): the embedding that model produced.
CREATE TABLE IF NOT EXISTS message_embeddings (
    message_id UUID   NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    model_name TEXT   NOT NULL,
    embedding  vector NOT NULL,
    PRIMARY KEY (message_id, model_name)
);

-- Per-model throughput metrics captured during the bakeoff run.
CREATE TABLE IF NOT EXISTS embedding_perf (
    model_name    TEXT NOT NULL PRIMARY KEY,
    msgs_embedded INTEGER,
    elapsed_s     NUMERIC,
    msgs_per_s    NUMERIC,
    recorded_at   TIMESTAMPTZ DEFAULT NOW()
);
