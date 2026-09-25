-- ============================================================================
-- V001__orchestrator.sql — SQLite equivalent of the orchestrator's
-- Postgres lease + outbox tables. Use this when --state-store=sqlite.
--
-- SQLite doesn't support schemas or RLS natively; the application enforces
-- multi-tenant isolation in Python. For single-tenant projects this is
-- sufficient.
-- ============================================================================

-- Lease table (mirrors Postgres BIGSERIAL with AUTOINCREMENT).
CREATE TABLE IF NOT EXISTS orchestrator_lease (
    ticket_id          TEXT PRIMARY KEY,
    tenant_id          TEXT,
    holder_id          TEXT NOT NULL,
    lease_expires_at   TEXT NOT NULL,
    fencing_token      INTEGER UNIQUE NOT NULL,
    claimed_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS orchestrator_lease_expires_idx
    ON orchestrator_lease (lease_expires_at);

-- Outbox table.
CREATE TABLE IF NOT EXISTS orchestrator_outbox (
    outbox_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    action_type      TEXT NOT NULL,
    path             TEXT,
    content          TEXT,
    diff             TEXT,
    enqueued_at      TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at       TEXT,
    applied_by       TEXT,
    tenant_id        TEXT
);

CREATE INDEX IF NOT EXISTS orchestrator_outbox_pending_idx
    ON orchestrator_outbox (outbox_id) WHERE applied_at IS NULL;
