-- ============================================================================
-- V001__lease.sql — Postgres-backed monotonic fencing-token lease.
--
-- Kleppmann-correct distributed coordination primitive (per Martin
-- Kleppmann's "How to do distributed locking", 2016). The previous lease
-- implementations used a UUID v4 string as the fencing token — UUID v4
-- has no ordering, so every comparison was a binary equality check, NOT
-- a monotonic ordering. Two latent failure modes:
--
--   1. The orchestrator's guard comment that checks `lease_token` could
--      only fail when the orchestrator read the same token twice in
--      succession. It did NOT protect against stale claims because
--      comparing two random UUIDs tells you nothing about which one was
--      issued later.
--   2. A long-running holder whose `lease_expires_at` slipped past `now`
--      could resume work after a new holder claimed the lease.
--
-- THE FIX
-- --------
-- Move lease state to a Postgres table with a `BIGSERIAL fencing_token`
-- column. The orchestrator's atomic claim becomes:
--
--   INSERT INTO orchestrator.lease (ticket_id, tenant_id, holder_id,
--                                   lease_expires_at, fencing_token)
--   VALUES (...)
--   ON CONFLICT (ticket_id) DO NOTHING
--   RETURNING fencing_token;
--
-- A stale-token UPDATE becomes `WHERE fencing_token > $claimed_token`,
-- which the storage layer rejects with 0 rows affected. This is the
-- canonical Chubby / Postgres advisory-lock pattern (sequence-numbered
-- fencing tokens issued at every state transition).
--
-- WHY BIGSERIAL
-- -------------
-- `BIGSERIAL` is the canonical Postgres sequence-backed monotonically-
-- increasing 64-bit integer. `BIGSERIAL UNIQUE NOT NULL` guarantees
-- strict monotonicity within a single Postgres instance (the sequence
-- never re-issues a value, never rolls back) and uniqueness across all
-- writers (no two claims share a token, even concurrent ones).
--
-- WHY (ticket_id) ALONE IS THE PRIMARY KEY
-- ----------------------------------------
-- A ticket has at most one live lease. `ticket_id TEXT PRIMARY KEY`
-- enforces the invariant at the storage layer: a second `claim` on the
-- same ticket either succeeds (if the existing lease has expired, after
-- the reaper deletes it) or fails the ON CONFLICT clause with zero rows
-- affected. This is the atomic "exactly one holder at a time" primitive.
--
-- WHY THE REAPER INDEX IS ON lease_expires_at
-- -------------------------------------------
-- `reap_stale()` runs `DELETE FROM orchestrator.lease WHERE
-- lease_expires_at < $now`. The index makes this O(stale-rows), not
-- O(total-rows).
-- ============================================================================

BEGIN;

-- Schema bootstrap (idempotent; matches same defensive pattern as V018/V024/V025).
CREATE SCHEMA IF NOT EXISTS orchestrator;

-- The lease table.
CREATE TABLE IF NOT EXISTS orchestrator.lease (
    ticket_id          TEXT PRIMARY KEY,
    tenant_id          UUID NOT NULL,
    holder_id          TEXT NOT NULL,
    lease_expires_at   TIMESTAMPTZ NOT NULL,
    fencing_token      BIGSERIAL UNIQUE NOT NULL,
    claimed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reaper hot-path index.
CREATE INDEX IF NOT EXISTS orchestrator_lease_lease_expires_at_idx
    ON orchestrator.lease (lease_expires_at);

-- Grants for the least-privileged application role.
-- The default role name is `orchestrator_app`; change via env var
-- ORCHESTRATOR_DB_USER + ORCHESTRATOR_DB_ROLE.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('orchestrator.app_role', true)) THEN
        EXECUTE format(
            'GRANT USAGE ON SCHEMA orchestrator TO %I; '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON orchestrator.lease TO %I; '
            'GRANT USAGE, SELECT ON SEQUENCE orchestrator.lease_fencing_token_seq TO %I;',
            current_setting('orchestrator.app_role', true),
            current_setting('orchestrator.app_role', true),
            current_setting('orchestrator.app_role', true)
        );
    ELSE
        RAISE NOTICE 'V001: skipping grants (orchestrator.app_role GUC not set; set it then re-apply V001)';
    END IF;
END $$;

-- Row-level security (optional — opt-in via orchestrator.rls_enabled GUC).
-- Per the orchestrator's design, RLS is ON when the project is multi-tenant
-- and OFF when it's single-tenant. Toggle by setting orchestrator.rls_enabled
-- to 'true' BEFORE applying this migration.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_tables
         WHERE schemaname = 'orchestrator' AND tablename = 'lease'
    ) AND current_setting('orchestrator.rls_enabled', true) = 'true' THEN
        EXECUTE $E$
            ALTER TABLE orchestrator.lease ENABLE ROW LEVEL SECURITY;
            ALTER TABLE orchestrator.lease FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS tenant_isolation_orchestrator_lease ON orchestrator.lease;
            CREATE POLICY tenant_isolation_orchestrator_lease ON orchestrator.lease
                USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
        $E$;
    ELSE
        RAISE NOTICE 'V001: skipping tenant_isolation_orchestrator_lease (RLS disabled or table missing)';
    END IF;
END $$;

COMMIT;
