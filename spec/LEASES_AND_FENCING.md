# Leases & Fencing Specification

## 1. Scope & Objective

This specification details the mathematical and practical mechanics of leases, heartbeats, ownership, and fencing tokens in the orchestrator.

We demonstrate why naive distributed locking fails, prove how monotonic fencing tokens guarantee correctness, and define the exact SQL and API semantics required.

---

## 2. Distinction of Core Concepts

```
+---------------------------------------------------------------------------------+
| LEASE                  | Time-bounded exclusive right to execute work.           |
| HEARTBEAT              | Liveness signal; NEVER used to prove safety.            |
| OWNERSHIP              | Record in DB: Owner(Work) = (WorkerId, FencingToken).   |
| FENCING TOKEN          | Strictly monotonic integer sequence number (41, 42...). |
| RESOURCE FENCE         | Check at storage barrier: reject if presented < active. |
+---------------------------------------------------------------------------------+
```

---

## 3. The Canonical Timeline: Stale Worker Rejection

```text
Time   Orchestrator DB             Worker A (PID 101)          Worker B (PID 102)
 |
00:00  Claim(Work-1)
       Token: 41 granted ---------> Received Token 41
                                    Starts execution...
00:05                               [PROCESS PAUSES: GC / OS Freeze]
 |                                  ================================
00:30  Lease Expires!
       (No heartbeat received)
 |
00:31  Reconciliation Loop
       Reclaims Work-1
 |
00:32  Claim(Work-1)
       Token: 42 granted -------------------------------------> Received Token 42
                                                                Starts execution...
00:40                                                           Finishes work.
                                                                Calls:
                                                                Submit(Token=42) ----> DB validates 42 >= 42
                                                                                       COMMITTED!
                                                                                       Token Watermark = 42
 |
01:00                               Worker A unfreezes!
                                    Believes it owns Work-1.
                                    Calls:
                                    Submit(Token=41) --------------------------------> DB checks: 41 < 42
                                                                                       REJECTED! (HTTP 409)
                                    Worker A aborts.                                   Rollback transaction.
```

---

## 4. Storage-Side Implementation

### 4.1 PostgreSQL Implementation
In PostgreSQL, the sequence is backed by a `BIGINT` sequence or `BIGSERIAL`:
```sql
CREATE SEQUENCE IF NOT EXISTS orchestrator_fencing_seq START 1;

CREATE TABLE IF NOT EXISTS orchestrator_lease (
    work_id          TEXT PRIMARY KEY,
    holder_id        TEXT NOT NULL,
    fencing_token    BIGINT NOT NULL DEFAULT nextval('orchestrator_fencing_seq'),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    acquired_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**Atomic Claim / Renewal Query:**
```sql
INSERT INTO orchestrator_lease (work_id, holder_id, fencing_token, lease_expires_at)
VALUES (:work_id, :holder_id, nextval('orchestrator_fencing_seq'), CURRENT_TIMESTAMP + :ttl_interval)
ON CONFLICT (work_id) DO UPDATE
SET holder_id = EXCLUDED.holder_id,
    fencing_token = nextval('orchestrator_fencing_seq'),
    lease_expires_at = EXCLUDED.lease_expires_at,
    acquired_at = CURRENT_TIMESTAMP
WHERE orchestrator_lease.lease_expires_at < CURRENT_TIMESTAMP
RETURNING fencing_token;
```

**Storage Fencing Verification on Write:**
```sql
UPDATE orchestrator_work
SET status = :new_status,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :work_id
  AND active_fencing_token = :presented_token;
-- If rows_affected == 0, transaction MUST be aborted with StaleFencingTokenError
```

### 4.2 SQLite Implementation
In SQLite, sequences are simulated using an atomic sequence table or row version counter inside a transaction:
```sql
BEGIN IMMEDIATE;
-- Advance fencing sequence
UPDATE orchestrator_sequence SET val = val + 1 WHERE name = 'fencing';
-- Fetch new token and claim lease
-- Commit transaction
COMMIT;
```

---

## 5. Token-Unaware Resources: Fenced Gateway Pattern

For resources that do not natively speak fencing tokens (local filesystems, Git repos, external APIs):

1. **Quarantine:** The worker is given an isolated temporary workspace path:
   $$\text{Path} = \text{/tmp/orchestrator/workbeds/work\_}\{id\}\text{/exec\_}\{token\}$$
2. **Gateway Promotion:** The worker cannot write to the main repository. When done, it generates a unified diff patch and calls the orchestrator's `PromoteArtifact` API, passing its `fencing_token`.
3. **Atomic Verification:** The orchestrator checks the token in the database. Only if the token is active does the orchestrator apply the patch to the main repository.
