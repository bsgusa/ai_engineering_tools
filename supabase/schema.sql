-- Supabase schema for the AI Engineering Toolkit pending-items tracker.
-- Apply this to your Supabase project. Service-role writes only; RLS is enabled.
--
-- psql or Supabase SQL Editor:
--   psql -h <supabase-host> -U postgres -f schema.sql
-- Or via the Supabase dashboard SQL Editor.

CREATE TABLE IF NOT EXISTS pending_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    score REAL NOT NULL DEFAULT 0,
    impact INTEGER NOT NULL DEFAULT 3,
    urgency INTEGER NOT NULL DEFAULT 3,
    effort INTEGER NOT NULL DEFAULT 3,
    suggested_action TEXT,
    draft TEXT,
    action_type TEXT,
    action_target TEXT,
    due TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_pending_items_status ON pending_items(status);
CREATE INDEX IF NOT EXISTS idx_pending_items_source ON pending_items(source);
CREATE INDEX IF NOT EXISTS idx_pending_items_score ON pending_items(score DESC);
CREATE INDEX IF NOT EXISTS idx_pending_items_due ON pending_items(due);
CREATE INDEX IF NOT EXISTS idx_pending_items_updated_at ON pending_items(updated_at DESC);

-- Row Level Security: service-role only for writes from this toolkit.
-- Your app (Lexora, etc.) can read with its own key; adjust as needed.
ALTER TABLE pending_items ENABLE ROW LEVEL SECURITY;

-- Service role can do everything (this toolkit uses the service key).
CREATE POLICY IF NOT EXISTS "service_role full access" ON pending_items
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Public read of open items (adjust if you want stricter visibility).
CREATE POLICY IF NOT EXISTS "authenticated read open items" ON pending_items
    FOR SELECT
    TO authenticated
    USING (status = 'open');

-- Helper: mark an item done
--   UPDATE pending_items SET status = 'done', updated_at = now() WHERE id = '...';

COMMENT ON TABLE pending_items IS 'Pending items tracked by the AI Engineering Toolkit. Local JSONL ledger is the source of truth; Supabase is a read mirror.'
