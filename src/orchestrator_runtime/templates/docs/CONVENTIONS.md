# Conventions

## File naming

| Type | Pattern | Example |
|------|---------|---------|
| Top-level | `UPPERCASE.md` | `STATE.md`, `WORKFLOW.md` |
| Ticket | `TKT-<PHASE>-<NNN>-<slug>.md` | `TKT-NNNpostgres-rls.md` |
| DevEx ticket | `TKT-NNN<NNN>-<slug>.md` | `TKT-NNNjustfile-fix.md` |
| BLOCKER | `BLOCKER-<N>-<slug>.md` | `BLOCKER-1-pen-test-closer.md` |
| Progress log | `progress/YYYY-MM-DD-<topic>.md` | `progress/2026-09-18-laptop-staging.md` |

Slugs are kebab-case, max 5 words.

## Status enum (fixed)

**Extended 2026-09-20 (Phase 3B) per Final Follow-Up Directive §5.** The state
machine is now 14 states (was 7). The validator lives at
`orchestrator/scripts/ticket_state_machine.py` and is enforced as actual
transition logic, not prose.

| Status | Meaning |
|--------|---------|
| **Upstream (pre-burn-queue)** | |
| `DISCOVERED` | Surfaced by `/orch-discover`, not yet a ticket. |
| `DRAFT` | Being written by `/orch-ticket`, not yet ready. |
| `READY` | Drafted, awaiting decision (auto-approve or human review). |
| `AWAITING_APPROVAL` | High-risk ticket in human review queue. Requires `approval_id` (per `approval_binding.py`). |
| `QUEUED` | Approved, in the burn queue. |
| **Mid-flight** | |
| `CLAIMED` | Picked up by an agent (lease acquired). |
| `EXECUTING` | Actively implementing. (Replaces the old `IN_PROGRESS` semantic.) |
| `VERIFYING` | 6-lens verifier running. |
| **Failure** | |
| `BLOCKED` | Cannot proceed; see BLOCKER file. |
| `PARTIAL` | Substantive progress, but specific items remain (file:line in ticket). |
| **Success** | |
| `DONE` | All AC satisfied + 3-perspective verified + `completed_at` set. Local-verified state; **not necessarily landed in a durable external record** — cross-reference via `landed_at` (see §Two-state semantics below). |
| **Downstream** | |
| `SHIPPED` | Landed in a durable external record (was `landed_at` semantics). |
| `DEPRECATED` | Superseded by a newer ticket; do not pick up. |
| `CANCELLED` | Decided to not pursue. Decision-Ledger entry required. |

**Allowed transitions.** The complete transition matrix is in
`orchestrator/scripts/ticket_state_machine.py`. Summary:

- **Upstream flow:** `DISCOVERED → DRAFT → READY → (AWAITING_APPROVAL | QUEUED) → QUEUED`
- **Mid-flight:** `QUEUED → CLAIMED → EXECUTING → VERIFYING → DONE`
- **Downstream:** `DONE → SHIPPED` or `DONE → DEPRECATED`
- **Cancellation:** allowed from any non-terminal state (with rationale)
- **Recovery:** `BLOCKED → QUEUED` (unblocked); `PARTIAL → EXECUTING` (resume)

**Illegal transitions (enforced by `validate_transition()`):**

- `READY → EXECUTING` (must go through QUEUED → CLAIMED first)
- `QUEUED → EXECUTING` (must go through CLAIMED first)
- `QUEUED → DONE` (must go through CLAIMED → EXECUTING → VERIFYING)
- `DISCOVERED → QUEUED` (must go through DRAFT → READY → QUEUED)
- `DRAFT → QUEUED` (must go through READY)
- `DONE → QUEUED`, `SHIPPED → EXECUTING`, `DEPRECATED → QUEUED`, `CANCELLED → QUEUED` (terminal states)
- `AWAITING_APPROVAL → QUEUED` without a current approval artifact bound to the ticket version

**Terminal states:** `CANCELLED`, `DEPRECATED`. No outbound edges.

**Approval requirement:** The transition `AWAITING_APPROVAL → QUEUED` requires
a current approval artifact (per Final Follow-Up Directive §4). The artifact
must be bound to the current `ticket_version`; see
`orchestrator/scripts/approval_binding.py` (Phase 3C).

Don't use ad-hoc statuses (`WIP`, `TODO`, `IN_REVIEW`, `MERGED`, `IN_PROGRESS`).
Stick to the enum. Note that `IN_PROGRESS` is replaced by `EXECUTING` in the new
schema; existing tickets using `IN_PROGRESS` should be migrated to `EXECUTING`
on next touch.

### Two-state semantics — `DONE` (local) vs `landed_at` (durable)

**TKT-NNN (2026-09-19).** Tickets can be `DONE` while the change has not been landed in any durable external record (e.g. project is not a git repo, or the user has not yet ratified the change, or the cloud deployment is pending). The two states are tracked by two distinct fields:

| Field | Set when | Means |
|---|---|---|
| `completed_at` | Status flips to `DONE` | All AC satisfied + 3-perspective verification passed + evidence recorded. **Local-verified state.** |
| `landed_at` | Change is in a durable external record | Either (a) a git commit (then `commit_ref` is also set), (b) the user has ratified the work in writing, or (c) the change has been deployed to a cloud environment. |

**Rule:** A ticket is `DONE` only after `completed_at` is set. `landed_at` is set **separately**, only when the change is in a durable external record. A `DONE` ticket may legitimately have `landed_at: null` — that is the expected state in environments without a git repo or before user ratification. Reading a ticket tomorrow, `completed_at` proves the work was verified; `landed_at` (when non-null) proves the work is durable.

**Schema:** `landed_at: <ISO timestamp | null>`. Type and nullability are mandatory.

## Frontmatter (ticket files)

```yaml
---
id: TKT-NNN
title: Postgres multi-tenant schema with RLS
phase: P0
priority: P0
status: DONE
created: 2026-09-13
updated: 2026-09-19
owner: orch-platform-team
adr_refs: [the project's decision records, the project's decision records, the project's decision records]
prd_ref: PRD-MOD-OPS-001
wave: 5
estimated_effort: 1w
depends_on: []                       # see §Dependency declaration below
commit_ref: <sha>
landed_at: <ISO timestamp | null>    # set ONLY when change is durable (git commit, user ratification, cloud deploy); see §Two-state semantics

# Lease lock (mandatory for IN_PROGRESS / PARTIAL; set by /orch-work or /orch-burn-queue)
picked_up_by: <agent-id>           # who holds the lease
picked_up_at: <ISO timestamp>      # when the lease was acquired
lease_expires_at: <ISO timestamp>  # when the lease expires (picked_up_at + TTL)
lease_token: <uuid-v4>             # fencing token for compare-and-swap
lease_ttl_minutes: 15              # default; extendable via heartbeat

# Completion metadata (set on DONE)
completed_at: <ISO timestamp>

verification:                      # Wave 2.5 3-perspective (voting + loop-until-PASS)
  correctness: PASS
  per_tenant: PASS
  failure_modes: PASS
  retries: 0                       # how many fix-and-re-verify cycles
  verified_at: 2026-09-19T...
  evidence:                        # file:line proof per perspective
    - { file: db/migrations/V001__init.sql, line: 42, note: "RLS enabled + forced" }
    - { file: ... }
  agent_count: 7                   # total sub-agents dispatched for tdomain-specific ticket
  pattern: [sectioning, orchestrator-workers, voting]
---
```

### Dependency declaration — `depends_on:` (TKT-NNN)

Cross-phase dependencies are first-class ticket metadata. Every ticket carries a `depends_on:` frontmatter field that lists the ticket IDs whose `status: DONE` must precede a claim of tdomain-specific ticket. The dependency audit (`orchestrator/scripts/dependency_audit.py`) enforces tdomain-specific at every pre-claim gate.

**Schema:**

```yaml
depends_on: [TKT-NNN, TKT-NNN]   # explicit upstream tickets
# or
depends_on: []                              # no upstream deps (still explicit)
```

Inline YAML list. Ticket IDs use the canonical `TKT-<PHASE>-<NNN>` form (e.g. `TKT-NNN`, `TKT-NNN`). Order is not significant; the audit treats the field as a set.

**Rule:** A ticket with `status: QUEUED` is claimable only if it satisfies **either**:

1. `depends_on:` is present (with at least one entry, OR an explicit empty list — absence vs. `[]` is the distinction: missing is treated as BLOCKED, empty list is treated as OK), OR
2. `phase:` is in the exempt set `{P0, P1}` — by construction every upstream predecessor is already landed before any P0/P1 ticket is filed, so an explicit list carries no useful signal.

**When to set `depends_on:`:**

- File the ticket alongside any predecessor (R1 retry tickets, follow-ups). Include every ticket that must be DONE before the claim is valid.
- Cross-phase dependencies: a P3 ticket that depends on a P1 ticket must list it explicitly. The phase-ordering heuristic in `burn-queue §2` is a fallback, NOT a substitute.
- Empty list `[]`: only when the ticket genuinely has no upstream predecessor AND its phase is outside `{P0, P1}` (e.g. orch-self cleanup work). The empty list is an explicit "I have thought about tdomain-specific and there are no deps" declaration — distinct from "field omitted".

**Audit invocation (per `WORKFLOW.md §Step 2.0`):**

```bash
# Single ticket pre-claim check.
python3 orchestrator/scripts/dependency_audit.py TKT-NNN
# OK: TKT-NNN dependencies satisfied                    # exit 0 — claim
# BLOCKED: TKT-NNN missing depends_on declaration       # exit 1 — fix first

# Global sweep across every QUEUED ticket.
python3 orchestrator/scripts/dependency_audit.py
# ... summary table; exit 0 if all OK, exit 1 if any BLOCKED
```

**Remediation for a BLOCKED ticket:**

1. Add `depends_on: [TKT-NNN, ...]` to the frontmatter, OR
2. Set `depends_on: []` if the ticket truly has no upstream deps (must be conscious decision), OR
3. Move the ticket to phase `P0` / `P1` if appropriate.

**Pre-existing usage examples:**

- `TKT-NNNmllp-tls-retry.md` → `depends_on: [TKT-NNN]`
- `TKT-NNNoffline-sync-retry.md` → `depends_on: [TKT-NNN, TKT-NNNa, TKT-NNNb, TKT-NNNc, TKT-NNNd]`
- `TKT-NNNhl7-simulator-retry.md` → `depends_on: [TKT-NNN, TKT-NNN]`

### Ticket lease lock — Postgres-backed monotonic fencing-token protocol (TKT-NNN)

The orchestrator prevents **two agents from working the same ticket** with a **lease-based lock** (Kleppmann monotonic fencing-token pattern; Chubby / Postgres advisory-lock shape). Tdomain-specific works across multiple orchestrator sessions on the same machine (e.g., two `/orch-work` invocations, or `/orch-work` + `/orch-burn-queue` running simultaneously).

**Source of truth — Postgres, NOT the markdown file.** The frontmatter `lease_*` fields are a **derived cache** (regenerated from Postgres by `lease.py read_lease(ticket_id)` — see `orchestrator/scripts/lease.py:556`), NOT authoritative. Every claim / heartbeat / release writes to Postgres FIRST; the markdown cache is updated only on success. The pre-existing inline frontmatter-write pattern (UUID v4 + guard comment) is **REMOVED** — UUID v4 has no order and cannot implement Kleppmann's monotonic-fencing-token argument.

**Authoritative storage — `orchestrator.lease` table** (migration `db/migrations/V024__orchestrator_lease.sql`):

| Column | Type | Purpose |
|---|---|---|
| `ticket_id` | `TEXT PRIMARY KEY` | One lease per ticket |
| `tenant_id` | `UUID NOT NULL` | RLS key (per-tenant isolation, canonical NULLIF form) |
| `holder_id` | `TEXT NOT NULL` | Identifies the lease holder |
| `lease_expires_at` | `TIMESTAMPTZ NOT NULL` | Hard expiry; after tdomain-specific, the lease is reapable |
| `fencing_token` | `BIGSERIAL UNIQUE NOT NULL` | Monotonic — every claim / heartbeat increments it |
| `claimed_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | Audit |

RLS is enabled + forced via `tenant_isolation_orchestrator_lease` policy. Reaper sweep is supported by `orchestrator_lease_lease_expires_at_idx`.

**Adapter module — `orchestrator/scripts/lease.py`:**

| Function | Purpose |
|---|---|
| `claim(ticket_id, tenant_id, holder_id, ttl_minutes) -> int` | `INSERT ... RETURNING fencing_token`. Raises `LeaseHeldError(holder_id, lease_expires_at)` if held. — `lease.py:280` |
| `heartbeat(ticket_id, holder_id, fencing_token) -> int` | Extends `lease_expires_at`; rotates `fencing_token` (monotonic +1 per heartbeat); returns new token. Rejects with `StaleFencingTokenError` if the supplied token is not the current max. — `lease.py:380` |
| `release(ticket_id, holder_id, fencing_token) -> None` | `DELETE ... WHERE fencing_token = $claimed`. Rejects stale. — `lease.py:483` |
| `reap_stale(now) -> int` | Deletes leases where `lease_expires_at < now`. Returns count. — `lease.py:606` |
| `read_lease(ticket_id) -> LeaseRow \| None` | Reads from Postgres; used to refresh the markdown cache. — `lease.py:556` |

Uses the existing `orchestrator_app` Postgres role (least-privileged), sets `app.current_tenant` GUC before each query (`lease.py:265`). The interface is a **backward-compatible drop-in** for the old inline claim protocol — `WORKFLOW.md §Step 2` only needs a 1-paragraph update pointing at the module (TKT-NNN AC #4).

**Atomic claim protocol** (Postgres-side, executed by `lease.py claim()`):

1. **Read availability** via `SELECT FROM orchestrator.lease WHERE ticket_id = $1 AND tenant_id = $2`:
   - No row → proceed to claim.
   - Row exists + `lease_expires_at < now` → **stale lease**; the reaper will delete it; the next claim inserts.
   - Row exists + `lease_expires_at >= now` → **held by another agent** (`holder_id`); raise `LeaseHeldError` (`lease.py:156`).
2. **Insert** (single Postgres statement, atomic):
   ```sql
   INSERT INTO orchestrator.lease (ticket_id, tenant_id, holder_id, lease_expires_at)
   VALUES ($1, $2, $3, now() + ($4 || ' minutes')::interval)
   RETURNING fencing_token;
   ```
   Postgres serializes concurrent inserts at the row level — exactly one wins.
3. **Update markdown cache** (best-effort, on claim success):
   - `status: QUEUED → IN_PROGRESS`
   - `picked_up_by: <holder_id>`
   - `picked_up_at: <now ISO>`
   - `lease_expires_at: <now + TTL>`
   - `lease_token: <fencing_token as decimal string>` (mirrored from Postgres)
   - `lease_ttl_minutes: <TTL>` (per §Auto-derived below)
4. **Heartbeat** — `lease.py heartbeat(...)` extends `lease_expires_at` and rotates `fencing_token` to a higher value. Any stale holder that issues a heartbeat with the old token gets `StaleFencingTokenError` (`lease.py:180`).

**Stale lease recovery (reaper):**

If `lease_expires_at < now` at claim time, `lease.py reap_stale(now)` deletes the row (also available as a periodic sweep). The orchestrator logs the reaper event: `progress/YYYY-MM-DD-<topic>.md` gains a row `[STALE-LEASE-REAPED] TKT-NNN by <new-holder-id> — previous holder <old-holder-id> lease expired at <old-expiry>`. The previous holder's `commit_ref` (if any) is preserved — the new agent reads the progress log and resumes from the last verified state.

**Atomic write protection (Kleppmann monotonic fencing token):**

The BIGSERIAL `fencing_token` enforces write ordering across the cluster. A stale-token heartbeat becomes `UPDATE orchestrator.lease SET lease_expires_at = ..., fencing_token = nextval(...) WHERE ticket_id = $1 AND fencing_token > $claimed` — Postgres rejects the UPDATE (0 rows affected) when the supplied token is not the current max. Tdomain-specific is the **monotonic-fencing-token pattern** — it prevents two agents from both successfully writing heartbeat / release to the same ticket even under racy read-modify-write cycles. The old guard-comment-with-UUID pattern is **REMOVED** because UUID v4 has no order.

**Why not a file lock (`flock`):** the orchestrator uses Postgres for lease state. `flock` would require an external lockfile per ticket, adding OS-level complexity, AND it would not survive a process crash between claim and heartbeat. Postgres gives crash-safe atomicity plus per-tenant RLS isolation for free (CLAUDE.md #2 alignment).

**Why not just trust `status: IN_PROGRESS`:** a single-field status check is racy under read-modify-write. Two agents could both read `status: QUEUED`, both decide to claim, both write `status: IN_PROGRESS`. The Postgres `INSERT ... RETURNING fencing_token` makes tdomain-specific impossible — the second INSERT collides on the `ticket_id` PRIMARY KEY and the orchestrator surfaces `LeaseHeldError`.

### Auto-derived `lease_ttl_minutes` from `estimated_effort` (TKT-NNN)

The default `lease_ttl_minutes: 15` is a floor, not a value. A 1-day ticket needs ~50 heartbeats; a 1-week ticket needs ~200+. Each heartbeat is a file write + token rotation — fragile. The orchestrator auto-derives the TTL from `estimated_effort`:

```
lease_ttl_minutes = max(15, ceil(estimated_effort_days * 60 * 24 * 0.25))   # capped at 60
```

Where `estimated_effort_days` is parsed from the `estimated_effort` frontmatter (e.g. `1w` → 5, `1d` → 1, `4h` → 0.5, `30m` → 0.021). The 0.25 factor is the **heartbeat-density** heuristic (K8s `Lease` uses a similar `durationSeconds / 3` for renew interval): one heartbeat per ~4 minutes of work. The result is floored at 15 (so trivial tickets don't lose the lease mid-flight) and capped at 60 (so a long-running ticket must heartbeat rather than hold a single multi-hour lease).

**Worked examples:**

| `estimated_effort` | `estimated_effort_days` | raw formula | `lease_ttl_minutes` |
|---|---|---|---|
| `30m` | 0.0208 | 7.5 | **15** (floor) |
| `4h` | 0.1667 | 60 | **15** (floor) |
| `1d` | 1 | 360 | **60** (cap — formula gives 360, capped at 60) |
| `1w` | 5 | 1800 | **60** (cap) |

The 1w ticket and the 1d ticket both saturate the cap at 60 minutes; they MUST heartbeat (`WORKFLOW.md §Step 2 — Heartbeat`) to extend the lease rather than holding one lease for the entire work. Heartbeat is mandatory on every commit, or every 10 minutes, whichever is sooner.

## Cascade marker convention (TKT-NNN)

The status + cascade update at `WORKFLOW.md §Step 6` is **not atomic**: it touches 5+ files (ticket frontmatter → `tickets-index.md` → progress log → `STATE.md` → `blockers-index.md` → commit) in sequence. If the orchestrator crashes between any two writes, the state is inconsistent — e.g. the ticket is `DONE` but `STATE.md` still shows it `IN_PROGRESS`.

The mitigation is **not transactional state** (the orchestrator is a file-system reconciler, not a database). It is a **level-triggered recovery marker**: a single marker file written BEFORE the cascade starts, deleted AFTER the cascade completes.

**Marker file naming:**

```
progress/.cascade-in-progress-<TKT-NNN>
```

- Written at the START of `WORKFLOW.md §Step 6` (step 6.0).
- Deleted at the END of `WORKFLOW.md §Step 6` (after step 6.8).
- Contains a JSON body with `{ "ticket_id": "TKT-NNN", "started_at": "<ISO>", "step": "<6.1|6.2|…>" }` so a human or recovery script can tell where the cascade was interrupted.

**Recovery protocol (per `progress/README.md`):**

If a `.cascade-in-progress-*` file is present at session start, that ticket claims DONE but the cascade was interrupted. The recovery steps:

1. Read the ticket file — is `status: DONE`? Was `completed_at` set?
2. Read `tickets-index.md` — is the ticket row updated to `DONE`?
3. Read `STATE.md` — is the at-a-glance count current?
4. Read `blockers-index.md` — if a BLOCKER step was closed, is the row updated?
5. Reconcile: complete any unwritten step from the cascade. Then DELETE the marker file.

**Industry analogue:** K8s uses a level-triggered reconciler that converges regardless of partial failures. For tdomain-specific orchestrator, the marker file is the equivalent — its presence is a "stale write" the next session reconciles.

## Cascade outbox protocol (TKT-NNN)

The cascade marker (TKT-NNN above) is a **recovery aid**, not a prevention. The cure for partial-cascade drift (Layer 8 P0-004 / P0-006 PARTIAL drift; PILOT-002 worker `a43ee5486552546a2` stalled mid-`_join` debug with ~1700 lines of uncommitted partial code) is a **transactional outbox**: every `WORKFLOW.md §Step 6` cascade action is written to `orchestrator.outbox` in a single Postgres transaction, then applied by an idempotent consumer (canonical microservices.io pattern, Chris Richardson).

**Authoritative storage — `orchestrator.outbox` table** (migration `db/migrations/V025__orchestrator_outbox.sql`):

| Column | Type | Purpose |
|---|---|---|
| `outbox_id` | `BIGSERIAL PRIMARY KEY` | Monotonic ordering — consumer applies in `outbox_id` order |
| `action` | `JSONB NOT NULL` | `{"type": "edit_file"\|"write_file"\|"git_commit", "path": "...", "content": "...", "diff": "..."}` |
| `idempotency_key` | `TEXT NOT NULL UNIQUE` | Catches duplicate enqueue; consumer re-applies safely |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | Ordering |
| `applied_at` | `TIMESTAMPTZ NULL` | Set by consumer on success |
| `applied_by` | `TEXT NULL` | Which consumer / sub-agent applied it |

RLS enabled + forced via per-tenant policy using canonical NULLIF. Index `orchestrator_outbox_unapplied_idx` on `(created_at) WHERE applied_at IS NULL` for the consumer sweep.

**Action schema** (the `action` JSONB):

```json
{
  "type": "edit_file|write_file|git_commit",
  "path": "<relative path under repo root>",
  "content": "<string for write_file / git_commit message>",
  "diff": "<unified diff string for edit_file / git_commit diffstat>"
}
```

The consumer dispatches by `type`. `edit_file` applies a unified diff with `git apply` (`outbox_consumer.py:197`); `write_file` overwrites the file atomically (`outbox_consumer.py:158`); `git_commit` runs `git commit -m <content>` after prior edit_file rows in the same cascade have been written (`outbox_consumer.py:236`).

**Adapter modules — `orchestrator/scripts/outbox.py` (writer) + `orchestrator/scripts/outbox_consumer.py` (consumer):**

`outbox.py`:

| Function | Purpose |
|---|---|
| `enqueue(action: dict, idempotency_key: str, tenant_id: uuid) -> int` | Inserts a row. The `idempotency_key` UNIQUE constraint catches duplicate enqueue (`DuplicateEnqueueError`). — `outbox.py:245` |
| `pending(limit: int = 100) -> list[OutboxRow]` | Returns unapplied rows ordered by `outbox_id`. — `outbox.py:312` |
| `mark_applied(outbox_id: int, applied_by: str) -> None` | Sets `applied_at` + `applied_by`. — `outbox.py:392` |
| `stats()` | Snapshot for `regenerate_metrics.py` (outbox pending count + applied/min). — `outbox.py:464` |

`outbox_consumer.py`:

- `consume_pending(...)` loops: read pending → for each row, dispatch by `action.type` → `mark_applied`. — `outbox_consumer.py:307`
- `run_forever(...)` is the long-running dispatcher (sub-agent or background process per layer). — `outbox_consumer.py:477`
- **Idempotent**: same `idempotency_key` cannot be applied twice (UNIQUE constraint + pre-apply check).
- **Crash-safe**: if consumer crashes mid-apply, the row stays unapplied; next restart picks it up. `mark_applied` is the commit point — if it fails after the file write succeeded, the next sweep sees `applied_at IS NULL` and re-applies (the action's `idempotency_key` UNIQUE + the file's eventual content make tdomain-specific a no-op for `write_file`, a successful no-op for `edit_file` if the diff is already applied).

**Step 6 cascade rewrite — `WORKFLOW.md §Step 6`:**

Each cascade sub-action (ticket frontmatter update + `tickets-index.md` + progress log + `STATE.md` + `blockers-index.md` + commit) is enqueued to the outbox in a single transaction:

```python
from orchestrator.scripts.outbox import enqueue

with conn.begin():  # single transaction — atomic across all sub-actions
    for sub_action in cascade:
        enqueue(
            action=sub_action,
            idempotency_key=f"{ticket_id}:{sub_action['path']}:{sub_action['type']}",
            tenant_id=current_tenant,
        )
# commit point
```

The consumer applies them in `outbox_id` order. If the orchestrator crashes between enqueue and consumer run, the next session detects unapplied rows via `SELECT * FROM orchestrator.outbox WHERE applied_at IS NULL` and reconciles.

**Why not a single multi-file Edit:** the Edit tool is per-file atomic, but the cascade as a whole is not atomic across files. Crash between any two Edits leaves partial state visible to the next agent (tdomain-specific is the failure mode observed in Layer 8 + the PILOT-002 worker stall). The outbox turns the cascade into a single Postgres transaction on the enqueue side + an idempotent consumer on the apply side.

**Backward compatibility.** The TKT-NNN marker-file convention (above) is PRESERVED as a defense-in-depth — the outbox is the prevention, the marker is the recovery aid for cascades that pre-date the outbox migration or for partial-failure forensic dumps.

## Sub-agent return schemas (canonical)

Every sub-agent the orchestrator dispatches returns one of these shapes. Pass `schema:` (JSON Schema) at the dispatch site — validation happens at the tool layer; sub-agents retry on mismatch.

### `WORKER_RESULT` (from `general-purpose` implementation workers)

**Note (TKT-NNN, 2026-09-19):** `verifier_provenance` is REQUIRED. The orchestrator validates that 3 distinct sub-agent IDs issued the 3 lens verdicts. Self-attestation is FORBIDDEN — Layer 8 caught 2 of 3 workers doing tdomain-specific, with 5 invariants that would have silently shipped.

```json
{
  "type": "object",
  "required": ["files_changed", "verification", "verifier_provenance"],
  "properties": {
    "files_changed": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["path", "summary"],
        "properties": {
          "path":     { "type": "string" },
          "summary":  { "type": "string", "maxLength": 500 },
          "ac_bullets_covered": { "type": "array", "items": { "type": "string" } }
        }
      }
    },
    "tests_added": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path":      { "type": "string" },
          "test_name": { "type": "string" }
        }
      }
    },
    "verification": {
      "type": "object",
      "required": ["correctness", "per_tenant", "failure_modes", "reversibility", "retries", "verdict_sources"],
      "properties": {
        "priority":        { "type": "string", "enum": ["P0", "P1", "P2", "P3"], "description": "TKT-NNN: ticket priority at dispatch time — drives which lenses are required (see verifier_provenance.dispatched_count below)" },
        "correctness":    { "type": "string", "enum": ["PASS", "FAIL"] },
        "per_tenant":     { "type": "string", "enum": ["PASS", "FAIL"] },
        "failure_modes":  { "type": "string", "enum": ["PASS", "FAIL"] },
        "reversibility":  { "type": "string", "enum": ["PASS", "FAIL", "N/A"], "description": "TKT-NNN: PASS = reversible (rollback path exists + tested); FAIL = irreversible action without approval gate; N/A = ticket is non-mutating (no rollback needed)" },
        "security":       { "type": "string", "enum": ["PASS", "FAIL", "N/A"], "description": "TKT-NNN: STRIDE check — SQLi / XSS / secrets in logs / authz bypass / SSRF / log injection. P0/P1 MUST emit PASS or FAIL; P2/P3 may emit N/A." },
        "performance":    { "type": "string", "enum": ["PASS", "FAIL", "N/A"], "description": "TKT-NNN: scales to N=1/10/100 domain-specifics? Checks O(N^2) loops on tenant-scoped data, missing DB index on hot path, unbounded result sets, N+1 queries, missing connection pool. P0/P1 MUST emit PASS or FAIL." },
        "idempotency":    { "type": "string", "enum": ["PASS", "FAIL", "N/A"], "description": "TKT-NNN: re-run with same input → same output? Checks idempotency_key on side-effects, retry double-application, INSERT...ON CONFLICT for upserts, DLQ semantics for async work. Opt-in per ticket." },
        "retries":        { "type": "integer", "minimum": 0 },
        "verified_at":    { "type": "string", "format": "date-time" },
        "evidence": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["file", "line", "note"],
            "properties": {
              "file": { "type": "string" },
              "line": { "type": "integer", "minimum": 1 },
              "note": { "type": "string", "maxLength": 500 }
            }
          }
        },
        "verdict_conflict": {
          "type": "object",
          "description": "TKT-NNN (2026-09-22). Optional. Present only when the worker had to break a tie between two verifier dispatches (one PASS, one FAIL) OR when a single FAIL was classified as substrate drift (PARTIAL_WITH_FOLLOW_UPS). Schema is the canonical record of the resolution decision.",
          "properties": {
            "first_verdict":         { "type": "string", "enum": ["PASS", "FAIL"], "description": "The first verifier dispatch verdict" },
            "second_verdict":        { "type": "string", "enum": ["PASS", "FAIL", null], "description": "The second verifier dispatch verdict (null if the conflict is between a single FAIL and a single PASS from one retry)" },
            "scope_difference_note": { "type": "string", "description": "Free text — what scopes the two verdicts cover" },
            "chosen_resolution":     { "type": "string", "enum": ["LOOP_UNTIL_PASS", "PARTIAL_WITH_FOLLOW_UPS", "AMBIGUOUS"], "description": "The resolution the worker chose — must match one of the three RESOLUTION_* constants in orchestrator/scripts/verdict_conflict.py" },
            "rationale":             { "type": "string", "description": "Free text — why the chosen_resolution is right" }
          },
          "required": ["first_verdict", "chosen_resolution"]
        },
        "commit_ref":     { "type": ["string", "null"] },
        "verdict_sources": {
          "type": "object",
          "required": ["correctness_agent", "per_tenant_agent", "failure_modes_agent", "reversibility_agent"],
          "properties": {
            "correctness_agent":     { "type": "string", "description": "Sub-agent ID that issued the correctness verdict" },
            "per_tenant_agent":      { "type": "string", "description": "Sub-agent ID that issued the per_tenant verdict" },
            "failure_modes_agent":   { "type": "string", "description": "Sub-agent ID that issued the failure_modes verdict" },
            "reversibility_agent":   { "type": "string", "description": "TKT-NNN: sub-agent ID that issued the reversibility verdict" },
            "security_agent":        { "type": "string", "description": "TKT-NNN: sub-agent ID that issued the security verdict (P0/P1 only)" },
            "performance_agent":     { "type": "string", "description": "TKT-NNN: sub-agent ID that issued the performance verdict (P0/P1 only)" },
            "idempotency_agent":     { "type": "string", "description": "TKT-NNN: sub-agent ID that issued the idempotency verdict (opt-in)" }
          }
        }
      }
    },
    "verifier_provenance": {
      "type": "object",
      "required": ["dispatched_count", "sub_agent_ids", "priority"],
      "properties": {
        "priority":         { "type": "string", "enum": ["P0", "P1", "P2", "P3"], "description": "TKT-NNN: ticket priority at dispatch time. Drives the minimum verifier count and lens set below." },
        "dispatched_count": { "type": "integer", "minimum": 4, "description": "TKT-NNN: P2/P3 MUST be >= 4 (correctness + per_tenant + failure_modes + reversibility). P0/P1 MUST be >= 6 (adds security + performance; idempotency is opt-in and does not count toward the floor). The previous minimum of 3 was raised to 4 because TKT-NNN added the reversibility lens and the floor must move with it." },
        "sub_agent_ids":    { "type": "array", "minItems": 4, "uniqueItems": true, "items": { "type": "string" }, "description": "TKT-NNN: MUST contain at least 4 distinct sub-agent IDs for P2/P3 (correctness/per_tenant/failure_modes/reversibility) and at least 6 for P0/P1 (adds security + performance). Reuse of a verifier ID is FORBIDDEN per TKT-NNN — `uniqueItems: true` is enforced at the JSON Schema layer with a dedicated `REUSED_VERIFIER_ID` rejection code." }
      }
    },
    "blockers_discovered": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["ticket_id", "reason"],
        "properties": {
          "ticket_id": { "type": "string" },
          "reason":    { "type": "string" }
        }
      }
    },
    "final_status": { "type": "string", "enum": ["DONE", "PARTIAL", "BLOCKED"] }
  }
}
```

**Orchestrator validation (mandatory, see `orch-burn-queue.md` §6 + `orch-work.md` §6):**
1. `verifier_provenance.dispatched_count` MUST be **>= 4 for P2/P3** (correctness + per_tenant + failure_modes + reversibility) and **>= 6 for P0/P1** (adds security + performance). The previous floor of 3 was raised to 4 by TKT-NNN (TKT-NNN had added the 4th reversibility lens; TKT-NNN added 2 more for P0/P1).
2. `verifier_provenance.sub_agent_ids` MUST contain **at least 4 distinct values for P2/P3** and **at least 6 distinct values for P0/P1** (Set semantics, not List). The schema is `uniqueItems: true` — TKT-NNN (2026-09-19) closes that loophole with a dedicated `REUSED_VERIFIER_ID` rejection code. Two (or more) of the verifier IDs being identical means the worker dispatched the same agent multiple times — that is verifier reuse, NOT independent adversarial review.
3. `verification.verdict_sources.correctness_agent`, `per_tenant_agent`, `failure_modes_agent`, `reversibility_agent` MUST all be distinct (no self-attestation; a worker ID MUST NOT appear as its own verdict source). For P0/P1 tickets, `security_agent` and `performance_agent` MUST also be distinct and non-self-attesting.
4. Each `verdict_sources.*_agent` MUST match one of the IDs in `verifier_provenance.sub_agent_ids`.
5. **Priority rule (TKT-NNN):** `WORKER_RESULT.verification.priority == "P0" | "P1"` REQUIRES at least 6 verifier IDs (dispatched_count >= 6). `priority == "P2" | "P3"` REQUIRES at least 4. A P0/P1 WORKER_RESULT with fewer than 6 IDs is REJECTED with `INSUFFICIENT_VERIFIER_DISPATCH_FOR_PRIORITY`.
6. **If validation FAILS: REJECT the WORKER_RESULT.** The ticket stays at its pre-worker status. Orchestrator MAY independently dispatch the required lens set as fallback.

### `verdict_conflict` (optional field on `WORKER_RESULT.verification` — TKT-NNN, 2026-09-22)

**Purpose.** The Wave 2.5 loop-until-PASS pattern (TKT-NNN) is correct for **implementation defects** (FAIL with evidence INSIDE the ticket's call-site scope). It is **wrong** when a FAIL surfaces **cross-cutting substrate drift OUTSIDE the ticket's call-site scope** — calling those failures "implementation defects to loop until PASS" silently ships broken runtime. Layer 29-31 evidence (TKT-NNNb, TKT-NNNb-FOUND-003, TKT-NNN) shows tdomain-specific pattern.

The `verdict_conflict` field is the audit record of a resolution decision between (a) two conflicting verifier dispatches of the same lens, OR (b) a single FAIL classified as substrate drift (PARTIAL + follow-ups) by `verdict_conflict.classify_verdict_failure(...)`. See `orchestrator/WORKFLOW.md §Step 5.2` for the mechanical rule.

**When to populate the field:**

- The worker dispatched a lens twice and got conflicting verdicts (one PASS, one FAIL) — populate with `first_verdict`, `second_verdict`, `chosen_resolution`, `rationale`.
- The worker got a single FAIL classified as `PARTIAL_WITH_FOLLOW_UPS` — populate with `first_verdict: "FAIL"`, `second_verdict: null`, `chosen_resolution: "PARTIAL_WITH_FOLLOW_UPS"`, `rationale` quoting the substrate scope.

**When NOT to populate:**

- All dispatches agree (all PASS, or all FAIL with consistent scope) → no conflict, field absent.
- The single FAIL was classified as `LOOP_UNTIL_PASS` (in-scope defect) → no conflict, field absent.

**Schema (full):**

```json
{
  "first_verdict": "PASS" | "FAIL",
  "second_verdict": "PASS" | "FAIL" | null,
  "scope_difference_note": "<free text>",
  "chosen_resolution": "LOOP_UNTIL_PASS" | "PARTIAL_WITH_FOLLOW_UPS" | "AMBIGUOUS",
  "rationale": "<free text>"
}
```

`chosen_resolution` MUST be one of the three constants exposed by `orchestrator/scripts/verdict_conflict.py`:

  - `LOOP_UNTIL_PASS` — implementation defect; loop until PASS, cap 3 (§Step 7 trigger #4).
  - `PARTIAL_WITH_FOLLOW_UPS` — substrate drift; worker files the follow-up tickets; ticket transitions to PARTIAL, retry counter NOT incremented.
  - `AMBIGUOUS` — scope unclear or no follow-ups; orchestrator surfaces per §Step 7.

**Worked example (Layer 29 case — TKT-NNNb):**

```json
{
  "first_verdict": "FAIL",
  "second_verdict": "PASS",
  "scope_difference_note": "first.scope=outside_ticket_scope (audit_outbox table missing + no drainer + idempotency middleware not mounted); second.scope=inside_ticket_scope (call-site + doctrine shape correct)",
  "chosen_resolution": "PARTIAL_WITH_FOLLOW_UPS",
  "rationale": "first FAIL had 3 follow_up_ticket_ids naming the substrate gaps; second PASS had no evidence. Per TKT-NNN, thorough FAIL overrides narrow PASS; ticket transitions to PARTIAL with 3 follow-up tickets (TKT-NNNb-FOUND-001/002/003)."
}
```

**Validation.** Use `verdict_conflict.validate_verdict_conflict_field(field)` from `orchestrator/scripts/verdict_conflict.py` to schema-validate the field at cascade-update time. Missing `first_verdict` or `chosen_resolution` → `REJECTED` with `REJ_VERDICT_CONFLICT_MALFORMED`.

### `VERIFIER_RESULT` (from the Wave 2.5 verifiers)

**Note (TKT-NNN, 2026-09-19):** The lens enum is extended from 3 to 7 values. P2/P3 tickets dispatch 4 lenses (correctness / per_tenant / failure_modes / reversibility, the latter added by TKT-NNN). P0/P1 tickets additionally dispatch security + performance; idempotency is opt-in per ticket. The dispatcher selects which lenses to run based on `WORKER_RESULT.verification.priority`.

```json
{
  "type": "object",
  "required": ["verdict", "lens", "evidence"],
  "properties": {
    "verdict":  { "type": "string", "enum": ["PASS", "FAIL"] },
    "lens":     { "type": "string", "enum": ["correctness", "per_tenant", "failure_modes", "reversibility", "security", "performance", "idempotency"], "description": "TKT-NNN: extended from 3 to 7. P2/P3 dispatch the first 4; P0/P1 dispatch 6 (adds security + performance); idempotency is opt-in per ticket." },
    "evidence": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["file", "line", "note"],
        "properties": {
          "file": { "type": "string" },
          "line": { "type": "integer", "minimum": 1 },
          "note": { "type": "string", "maxLength": 500 }
        }
      }
    },
    "fix_suggestion": { "type": "string", "description": "Required when verdict=FAIL" }
  }
}
```

### `DISCOVERY_FINDING` (from `Explore` agents)

```json
{
  "type": "object",
  "required": ["concern", "findings"],
  "properties": {
    "concern": {
      "type": "string",
      "enum": ["code_reality", "spec_docs", "adjacent_risk", "per_tenant_boundary", "test_surface", "prior_art"]
    },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "file":     { "type": "string" },
          "line":     { "type": "integer" },
          "summary":  { "type": "string", "maxLength": 300 },
          "concern_specific": {
            "type": "object",
            "description": "Per-concern extra fields (e.g. callers[], ac_bullet_id, tenant_risk_level)"
          }
        }
      }
    }
  }
}
```

### `PLAN_RESULT` (from the one `Plan` agent)

```json
{
  "type": "object",
  "required": ["steps", "risk_register"],
  "properties": {
    "steps": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["order", "worker_id", "files", "ac_bullets"],
        "properties": {
          "order":      { "type": "integer", "minimum": 1 },
          "worker_id":  { "type": "string", "description": "Identifier for the general-purpose worker" },
          "files":      { "type": "array", "items": { "type": "string" } },
          "ac_bullets": { "type": "array", "items": { "type": "string" } },
          "depends_on": { "type": "array", "items": { "type": "integer" } }
        }
      }
    },
    "risk_register": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "risk":      { "type": "string" },
          "severity":  { "type": "string", "enum": ["low", "medium", "high"] },
          "mitigation":{ "type": "string" }
        }
      }
    },
    "parallel_groups": {
      "type": "array",
      "description": "Worker indices that may run in parallel without file collisions",
      "items": { "type": "array", "items": { "type": "integer" } }
    }
  }
}
```

## Commit message format (enhanced)

```
[TKT-<id>] <verb> <what changed>

- Acceptance criterion 1: satisfied at <file:line>
- Acceptance criterion 2: satisfied at <file:line>
- 3-perspective verification: correctness ✓ / per-tenant ✓ / failure-modes ✓ (retries: <N>)
- Sub-agent fan-out: <N explore> / <N workers> / <N plan> / <N verifiers> — peak <N> concurrent

Co-Authored-By: Claude Code <noreply@anthropic.com>
```

Example:

```
[TKT-NNN] land Postgres RLS + per-tenant isolation

- AC1 (RLS enabled + forced on tenants): db/migrations/V001__init.sql:42
- AC2 (tenant_id first PK column on all clinical tables): db/migrations/V001__init.sql:18-29
- 3-perspective verification: correctness ✓ / per-tenant ✓ / failure-modes ✓ (retries: 0)
- Sub-agent fan-out: 4 explore / 2 workers / 1 plan / 3 verifiers — peak 6 concurrent

Co-Authored-By: Claude Code <noreply@anthropic.com>
```

## Status update format (in index files)

| TKT-NNN | ✅ DONE | Postgres RLS | 2026-09-16 |

Use emoji prefixes for visual scan:

- ✅ DONE
- 🟡 YELLOW / PARTIAL
- ❌ RED
- ⏳ QUEUED
- 🔵 IN_PROGRESS
- ⚠️ BLOCKED

## Date format

ISO 8601 throughout:

- Dates: `YYYY-MM-DD` (e.g. `2026-09-19`)
- Timestamps: `YYYY-MM-DDTHH:MM:SSZ` (e.g. `2026-09-19T14:30:00Z`)

## Truth discipline (CLAUDE.md #3 spirit + V7 + V3 + V4)

- ❌ No fabricated NSSF claim-format specifics (per V7 + RK-0008-rerun).
- ❌ No fabricated clinical-AI evidence (per V3 — reject Lunit Lancet 2024 / Schalekamp).
- ❌ No fabricated vendor claims (per V4 InterSystems MENA).
- ❌ No fabricated Lebanon MOPH Vision 2030 citations (per V7).
- ❌ No claimed "tests pass" without the test runner output path recorded.
- ✅ When you don't know, write: "details in `<canonical-doc>` line X-Y" — never invent.

## Acceptance Criteria — mandatory smoke-test bullet

Every ticket that introduces new behavior (a NEW script, NEW endpoint, NEW migration, NEW verifier, NEW prompt) **must** include an explicit acceptance-criteria bullet that pins the behavior with an automated test. The test should be:

- A unit or integration test in `tests/` (Python pytest, Go `_test.go`, Java JUnit, TS Vitest — whatever fits the language).
- ≤ 30 lines per behavior, can be split across multiple test functions.
- Failure mode that exercises the negative path (e.g., for a "script accepts X" test, also test "script rejects not-X").

The `test_coverage` Wave 2.5 verifier lens (see `WORKFLOW.md §5`) **FAILs** any ticket missing tdomain-specific bullet. Tdomain-specific is non-negotiable. (Lesson learned 2026-09-19: the orchestrator self-improvement burn had 4 of 10 tickets reconcile tdomain-specific gap in fix-and-re-verify — adding the bullet to the convention prevents the gap from re-occurring.)

## File headers

Every ticket file ends with a `## Sources` section:

```markdown
## Sources

- `<project_root>/docs/07-tickets/slice-1-bed-management-tickets.md` (TKT-NNN)
- `<project_root>/db/migrations/V001__init.sql`
- `<project_root>/.commit-message-tkt-p0-001.txt`
```

Every BLOCKER file ends with `## Closure evidence` (for CLOSED ones) or `## What unblocks tdomain-specific` (for open ones).

## Priority semantics

| Priority | Meaning |
|----------|---------|
| `P0` | Blocks Wave 6 GREEN or blocks pilot go-live. Must close before downstream work. |
| `P1` | Required before multi-pilot onboarding. Due date tracked. |
| `P2` | DevEx debt, no production impact. |
| `P3` | Deferred to Wave 7 or later. |

## JWT verification contract (TKT-NNN)

Per `services/platform-api/internal/middleware/middleware.go` (lines 422-516) and `services/platform-api/internal/auth/jwt_keycloak.go`, the platform API's authentication middleware MUST enforce the following contract on **every non-bypass request**. The placeholder `c.Next()` (the original `middleware.go:228 / :250` TODO) is REMOVED — authn is no longer optional, and any ticket that re-introduces a placeholder or bypass is REJECTED by the 6-lens verifier suite's `security` lens (TKT-NNN).

**Required contract:**

1. **Bearer token parsing.** The middleware MUST extract a Bearer token from the `Authorization` header. Missing or malformed header → **401 Unauthorized** with body `{"code":"unauthorized","detail":"<reason>"}` (canonical error shape per `docs/03-architecture/security.md §1.5`). — `middleware.go:445`, `writeAuthFailure` at `middleware.go:515`.

2. **Signature verification (Keycloak JWKS).** Tokens MUST be verified against Keycloak JWKS (RS256 only, JWKS-pinned, issuer allow-list). On signature failure, the middleware MUST attempt a JWKS `ForceRefresh` ONCE and retry before failing. — `middleware.go:470` (ForceRefresh + retry), `jwks_cache.go:191` (`ForceRefresh`), `cmd/main.go:78-82` (`KEYCLOAK_JWKS_URL` wiring), `cmd/main.go:91-97` (`KEYCLOAK_ISSUER` trusted-issuer allow-list — realm-replay defense).

3. **`tenant_id` claim REQUIRED.** A valid signature without `tenant_id` → **403 Forbidden** with body `{"code":"missing_tenant","detail":"..."}` (`jwt_keycloak.go:155-160` — `ErrTokenMissingTenant`; `middleware.go:458`). Tdomain-specific is the cross-tenant authz bypass defense — without `tenant_id` there is no RLS context to enforce.

4. **`tenant_id` GUC propagation.** Before `c.Next()`, the middleware MUST set the `app.current_tenant` Postgres GUC via the connection wrapper (`tc.Set(c.Request.Context(), claims.TenantID)`) — `middleware.go:493`. All subsequent queries in the request inherit the per-tenant RLS context. **Per-tenant isolation depends on tdomain-specific; bypassing it is a P0 violation per CLAUDE.md #2.** Tdomain-specific was the regression Layer 8's over-dispatch caught (TKT-NNN context).

5. **Context population.** The middleware MUST populate: `c.Set(ContextKeyUser, claims)`, `c.Set("tenant_id", ...)`, `c.Set("subject", ...)`, `c.Set("claims", ...)` — `middleware.go:486-489`. Downstream handlers MUST read tenant from context, not from request data (defense against header-injection impersonation).

6. **Wire-in scope.** The middleware is mounted via the global `r.Use(...)` chain in `cmd/main.go:159-165`, so all 4 endpoint groups (auth / command / query / admin) flow through the new middleware consistently. Per-endpoint bypass is FORBIDDEN — a future ticket that adds a new endpoint group MUST mount the same middleware or be rejected by the security lens.

**Cross-tenant isolation defense.** The combination of (signature verification → `tenant_id` claim → GUC propagation → RLS forced on every clinical/business table per `db/migrations/V001__init.sql`) is the per-tenant boundary. The 6-lens verifier suite's `security` lens (TKT-NNN) is the runtime check: any future ticket that re-introduces a bypass, a missing claim, or a missing GUC set is FAILed before merge.

**Smoke tests** (per `CONVENTIONS.md §Acceptance Criteria`, NEW `services/platform-api/tests/jwt_middleware/jwt_middleware_test.go`):

| Test | Asserts |
|---|---|
| `TestJWT_ValidToken_SetsTenant` | Valid JWT → tenant context propagated to handler (`tenant_id` GUC set, `c.Set("tenant_id", ...)` populated). |
| `TestJWT_MissingToken_Returns401` | No `Authorization` header → 401 with canonical `{"code":"unauthorized","detail":"..."}`. |
| `TestJWT_InvalidSignature_Returns401` | Bad signature → 401 (after ForceRefresh + retry). |
| `TestJWT_MissingTenantClaim_Returns403` | Valid JWT but no `tenant_id` claim → 403 with `{"code":"missing_tenant","detail":"..."}`. |
| `TestJWT_JWKSRefresh_OnSignatureFailure` | Expired JWKS → ForceRefresh succeeds → retry validates. |
| `TestJWT_CrossTenantRequest_Blocked` | JWT for tenant A + endpoint under tenant B → 403 (RLS denies). |

Run: `cd services/platform-api && go test ./tests/jwt_middleware/... -v -run TestJWT_`.

## Date

2026-09-19.