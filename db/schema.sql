-- ============================================================
-- Demo application schema: a small e-commerce "orders" table.
-- This intentionally ships WITHOUT a secondary index on
-- (customer_id, order_date) -- that missing index is the
-- incident AgentOps is meant to discover and fix at demo time.
-- ============================================================

CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name STRING NOT NULL,
    region STRING NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id),
    order_date DATE NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    status STRING NOT NULL DEFAULT 'completed'
    -- NOTE: no index on (customer_id, order_date) on purpose.
);

-- ============================================================
-- AgentOps' own long-term memory: past incidents + resolutions,
-- searchable by vector similarity over a text description of
-- the symptoms (CockroachDB native VECTOR type + vector index).
-- ============================================================

CREATE TABLE IF NOT EXISTS incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT now(),
    symptom_text STRING NOT NULL,
    symptom_embedding VECTOR(384) NOT NULL,
    root_cause STRING,
    resolution_sql STRING,
    latency_before_ms FLOAT8,
    latency_after_ms FLOAT8,
    resolved BOOL DEFAULT false
);

CREATE VECTOR INDEX IF NOT EXISTS idx_incidents_embedding
    ON incidents (symptom_embedding);

-- ============================================================
-- Full agent execution trace -- this is what makes the system
-- explainable: every step every agent takes gets logged here
-- with a human-readable detail string, keyed to the incident.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_trace (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID,
    agent_name STRING NOT NULL,
    step STRING NOT NULL,
    detail STRING,
    data JSONB,               -- structured payload for the live dashboard
    created_at TIMESTAMPTZ DEFAULT now()
);

-- If the table pre-dates the `data` column (older demo runs), add it in place.
ALTER TABLE agent_trace ADD COLUMN IF NOT EXISTS data JSONB;

-- Replaying/streaming a trace filters by incident_id and orders by time.
-- Without this index that read is a full scan -- exactly the anti-pattern
-- this whole project exists to detect, so we don't ship it on our own table.
CREATE INDEX IF NOT EXISTS idx_agent_trace_incident
    ON agent_trace (incident_id, created_at);
