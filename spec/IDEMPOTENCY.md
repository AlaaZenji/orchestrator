# Idempotency Specification

## 1. Scope & Objective

Every state-mutating operation in the orchestrator must exhibit well-defined idempotency semantics. This prevents duplicate executions, phantom tasks, double-spending, and inconsistent state caused by network retries.

---

## 2. Idempotency Key Architecture

Every mutating API operation accepts an `Idempotency-Key` header or parameter $K \in \text{String}$.
The orchestrator maintains an `orchestrator_idempotency` table:

```sql
CREATE TABLE IF NOT EXISTS orchestrator_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    action_type     TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### Idempotency Contract:
1. **First Request:** Key $K$ is recorded in the transaction. Operation executes. Response status and body are saved.
2. **Duplicate Request with Identical Parameters:** Orchestrator returns cached response immediately with header `Idempotent-Replay: true`.
3. **Duplicate Request with Conflicting Parameters:** Orchestrator rejects with HTTP 422 Unprocessable Entity (`IDEMPOTENCY_KEY_PAYLOAD_MISMATCH`).

---

## 3. Operation-by-Operation Idempotency Matrix

| Operation | Canonical Idempotency Key Formula | Duplicate Request Behavior | Retry Behavior | Transaction Boundary |
| :--- | :--- | :--- | :--- | :--- |
| **Create Work** | `work:{work_id}` or client-supplied UUID | Returns existing `Work` record; does not create duplicate work. | Safe to retry infinitely on network timeout. | Single ACID transaction inserting into `work` and `outbox`. |
| **Claim Work** | `claim:{work_id}:{worker_id}:{lease_epoch}` | If active lease belongs to same worker, returns existing lease & token. If expired, issues new token. | Safe to retry. | Row lock `FOR UPDATE` on `lease` table. |
| **Start Execution**| `start_exec:{work_id}:{attempt}` | Returns existing `Execution` record with current status. | Safe to retry. | Insert into `execution` table. |
| **Tool Call** | `tool:{exec_id}:{step_index}:{tool_name}:{args_hash}`| Returns memoized tool response from audit log without re-executing tool. | Prevents duplicate shell or API side-effects. | Insert into `tool_audit` table. |
| **Artifact Creation**| `artifact:{exec_id}:{digest_sha256}` | Returns existing artifact metadata; storage deduplicates by content digest. | Safe to retry. | Insert into `artifact` table. |
| **Artifact Promotion**| `promote:{work_id}:{fencing_token}:{artifact_id}` | If already promoted, returns HTTP 200 OK. If token stale, returns HTTP 409 Conflict. | Idempotent CAS check. | CAS transaction updating `artifact` and target ref. |
| **Submit Verification**| `verify:{exec_id}:{verifier_name}` | Returns existing `VerificationRun` verdict if already completed. | Idempotent. | Insert into `verification_run` table. |
| **Approval** | `approval:{work_id}:{decision_nonce}` | If already approved, returns HTTP 200 OK. | Safe to retry. | Update `work` status to `RUNNING`. |
| **Complete Work** | `complete:{work_id}:{fencing_token}` | Returns HTTP 200 OK if already succeeded with matching token. | Safe to retry. | CAS update on `work` status guarded by `fencing_token`. |
| **Retry Work** | `retry:{work_id}:{failed_attempt}` | If already scheduled for retry, returns HTTP 200 OK with new attempt number. | Safe to retry. | Increment `retry_count`, reset lease, update status to `ELIGIBLE`. |
| **Cancel Work** | `cancel:{work_id}:{reason_hash}` | Returns HTTP 200 OK; work remains in `CANCELLED` status. | Safe to retry. | Update `work` status to `CANCELLED`; signal worker kill. |
