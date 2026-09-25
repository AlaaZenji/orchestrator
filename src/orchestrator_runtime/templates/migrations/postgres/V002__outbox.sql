-- ============================================================================
-- V002__outbox.sql — Transactional outbox for the cascade pattern.
--
-- The orchestrator's cascade (Step 6 of the workflow) executes 5+ separate
-- file edits (ticket frontmatter + tickets-index + progress log + state file
-- + blockers-index + commit). The Edit tool is per-file atomic, but the
-- cascade as a whole is NOT atomic across files. A crash between any two
-- edits leaves partial state visible to the next agent.
--
-- THE FIX
-- --------
-- Transactional outbox (canonical Chris Richardson microservices.io pattern):
-- every cascade action is written to `orchestrator.outbox` in a single
-- Postgres transaction, then applied by a consumer that processes outbox
-- rows atomically with idempotency. The cascade marker (recovery aid)
-- remains; the outbox is the prevention.
--
-- WHY A PARTIAL INDEX ON UNAPPLIED ROWS
-- -------------------------------------
-- The consumer reads `WHERE applied_at IS NULL ORDER BY outbox_id` on every
-- tick. After a long burn the table holds thousands of applied rows that
-- the consumer never touches again. A plain b-tree on `created_at` would
-- still scan past them; a partial index `WHERE applied_at IS NULL` indexes
-- ONLY the unapplied rows — every consumer tick reads from a tiny,
-- near-constant-size index.
--
-- WHY idempotency_key IS UNIQUE
-- -----------------------------
-- The `idempotency_key` UNIQUE constraint is the application-level dedup
-- primitive: a duplicate enqueue returns the row's outbox_id (same key
-- maps to the same BIGSERIAL row), and the consumer's `mark_applied`
-- short-circuits when `applied_at IS NOT NULL`. Operator actions must
-- NEVER be lost (per the orchestrator's no-irreversible-action invariant).
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS orchestrator;

CREATE TABLE IF NOT EXISTS orchestrator.outbox (
    outbox_id        BIGSERIAL PRIMARY KEY,
    tenant_id        UUID NOT NULL,
    action           JSONB NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at       TIMESTAMPTZ NULL,
    applied_by       TEXT NULL
);

-- The consumer's hot path: every tick reads from this partial index.
CREATE INDEX IF NOT EXISTS orchestrator_outbox_unapplied_idx
    ON orchestrator.outbox (outbox_id)
    WHERE applied_at IS NULL;

-- Tenant-scoped admin queries.
CREATE INDEX IF NOT EXISTS orchestrator_outbox_tenant_idx
    ON orchestrator.outbox (tenant_id, outbox_id);

-- Grants (same defensive pattern as V001).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('orchestrator.app_role', true)) THEN
        EXECUTE format(
            'GRANT USAGE ON SCHEMA orchestrator TO %I; '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON orchestrator.outbox TO %I; '
            'GRANT USAGE, SELECT ON SEQUENCE orchestrator.outbox_outbox_id_seq TO %I;',
            current_setting('orchestrator.app_role', true),
            current_setting('orchestrator.app_role', true),
            current_setting('orchestrator.app_role', true)
        );
    ELSE
        RAISE NOTICE 'V002: skipping grants (orchestrator.app_role GUC not set)';
    END IF;
END $$;

-- RLS (opt-in via GUC, same as V001).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_tables
         WHERE schemaname = 'orchestrator' AND tablename = 'outbox'
    ) AND current_setting('orchestrator.rls_enabled', true) = 'true' THEN
        EXECUTE $E$
            ALTER TABLE orchestrator.outbox ENABLE ROW LEVEL SECURITY;
            ALTER TABLE orchestrator.outbox FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS tenant_isolation_orchestrator_outbox ON orchestrator.outbox;
            CREATE POLICY tenant_isolation_orchestrator_outbox ON orchestrator.outbox
                USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
        $E$;
    ELSE
        RAISE NOTICE 'V002: skipping tenant_isolation_orchestrator_outbox (RLS disabled or table missing)';
    END IF;
END $$;

COMMIT;
