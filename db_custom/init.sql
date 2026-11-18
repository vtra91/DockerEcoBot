CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS eco_assistant;

CREATE TABLE IF NOT EXISTS public.error_log (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now(),
    user_query TEXT,
    error_message TEXT NOT NULL,
    context JSONB,
    additional_info JSONB
);
