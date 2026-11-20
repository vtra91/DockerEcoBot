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

-- Case-insensitive access to object_properties keys (exact key wins).
CREATE OR REPLACE FUNCTION eco_assistant.prop_ci(p jsonb, k text) RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT COALESCE(
    CASE WHEN jsonb_typeof(p) = 'object' THEN p -> k END,
    (SELECT e.value FROM jsonb_each(CASE WHEN jsonb_typeof(p) = 'object' THEN p ELSE '{}'::jsonb END) e
      WHERE lower(e.key) = lower(k) ORDER BY e.key LIMIT 1))
$$;
CREATE OR REPLACE FUNCTION eco_assistant.prop_ci_text(p jsonb, k text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT eco_assistant.prop_ci(p, k) #>> '{}' $$;

