# Red Team 1 — "Kill This Architecture" (Distributed Systems)

## 1. Executive Summary & Objective

The objective of this red team is to prove that the proposed durable agent orchestration architecture is fundamentally broken from a distributed systems perspective. We attempt to construct execution traces that cause:
- Split-brain state mutations
- Stale writers corrupting shared resources
- Lost updates or lost work
- Infinite retry cascades and duplicate execution storms
- Inconsistent state reconstruction following process crashes.

---

## 2. Attack Vector 1: The "Token-Blind Filesystem" Attack

### The Attack Trace:
1. Orchestrator assigns `Work-1` to Worker A with `fencing_token = 100`.
2. Worker A creates an isolated git branch `orch/work-1/exec-1` and begins modifying files.
3. Worker A experiences an operating system freeze (e.g. host swapping, hypervisor live-migration, or heavy model compilation).
4. The Orchestrator's lease watchdog detects lease expiry at $t = t_{\text{expire}}$.
5. The Orchestrator cancels Execution 1, advances token to `101`, and assigns `Work-1` to Worker B.
6. Worker B executes, completes, commits to branch `orch/work-1/exec-2`, and the Fenced Gateway merges `orch/work-1/exec-2` into `main`.
7. Worker A unfreezes. Worker A believes its token is still valid. Worker A executes `git push origin main` or writes directly to the shared working directory!

### Vulnerability Analysis:
If the orchestrator relies on the worker to "play nice" and check its own token, or if the worker shares a filesystem directory with the host or other workers, **the fencing token fails completely**. Fencing tokens only protect systems that explicitly validate them.

### Mandatory Architectural Countermeasure:
1. **Physical Workspace Isolation:** Worker A must execute in an isolated filesystem namespace (container mount or independent temporary directory).
2. **Read-Only / No Direct Push Rights:** Worker A must **never possess git push credentials to the primary branch or remote repository**.
3. **Fenced Gateway as Sole Committer:** Only the Orchestrator Control Plane has write access to the shared git branch or artifact repository. Promotion requires calling the control plane's `PromoteExecution(work_id, token, patch)` endpoint, which executes an atomic CAS check on `fencing_token`. Stale Worker A's promotion call is rejected with HTTP 409.

---

## 3. Attack Vector 2: The "Split-Brain Dual Scheduler" Attack

### The Attack Trace:
1. Two orchestrator instances (Scheduler 1 and Scheduler 2) run concurrently against a Postgres database for high availability.
2. Both schedulers run a sweep: `SELECT id FROM work WHERE status = 'ELIGIBLE'`.
3. Both schedulers observe `Work-10` is eligible.
4. Scheduler 1 generates lease token 200; Scheduler 2 generates lease token 201.
5. Both dispatch workers simultaneously, running duplicate executions in parallel.

### Vulnerability Analysis:
If scheduling and lease acquisition are two separate non-transactional steps (Read $\rightarrow$ Modify $\rightarrow$ Write), a classic TOCTOU race condition exists.

### Mandatory Architectural Countermeasure:
Lease acquisition must be executed as a single atomic SQL statement using row-level locking and concurrency suppression:
```sql
UPDATE orchestrator_work
SET status = 'RUNNING',
    active_execution_id = :new_exec_id,
    active_fencing_token = nextval('orchestrator_fencing_seq'),
    lease_expires_at = CURRENT_TIMESTAMP + :ttl_interval,
    version = version + 1
WHERE id = (
    SELECT id FROM orchestrator_work
    WHERE status = 'ELIGIBLE'
    ORDER BY priority DESC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, active_fencing_token, active_execution_id;
```
`FOR UPDATE SKIP LOCKED` guarantees that:
- Exactly one scheduler claims `Work-10`.
- The other scheduler skips the locked row immediately without blocking and claims the next available work.
- Zero duplicate executions can be spawned.

---

## 4. Attack Vector 3: The "Outbox Dual-Write Failure" Attack

### The Attack Trace:
1. Worker completes work. Orchestrator executes:
   `UPDATE orchestrator_work SET status = 'SUCCEEDED' WHERE id = :id;` (Transaction commits).
2. Orchestrator attempts to publish `io.orchestrator.work.succeeded` event to RabbitMQ / Kafka / WebSocket.
3. The network drops or the orchestrator process is killed (SIGKILL) before the message is sent.
4. Result: The database shows the work succeeded, but downstream event consumers, dashboards, and dependent workflow steps never receive the event. State becomes permanently inconsistent.

### Mandatory Architectural Countermeasure:
**Strict Transactional Outbox:**
State mutations and event publication must occur within the **same atomic ACID database transaction**:
```sql
BEGIN;
UPDATE orchestrator_work SET status = 'SUCCEEDED', ... WHERE id = :id;
INSERT INTO orchestrator_outbox (event_id, event_type, payload, status)
VALUES (:event_id, 'io.orchestrator.work.succeeded', :payload, 'PENDING');
COMMIT;
```
A reliable background Outbox Relayer reads pending events from the table and publishes them with at-least-once delivery guarantees. Downstream consumers deduplicate using `event_id`.

---

## 5. Summary of Architecture Corrections
The architecture survives this red team **only if**:
1. All worker file mutations occur in isolated workspaces; the control plane is the sole fenced committer.
2. Work dispatch utilizes atomic `FOR UPDATE SKIP LOCKED` claims with database sequence generation.
3. State changes and domain events commit atomically via the Transactional Outbox pattern.
