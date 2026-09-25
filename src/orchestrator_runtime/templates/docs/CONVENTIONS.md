# Conventions

> Status enum, ticket schema, lease protocol, commit format. Domain-agnostic.

## File naming

| Type | Pattern | Example |
|---|---|---|
| Top-level | `UPPERCASE.md` | `STATE.md`, `WORKFLOW.md` |
| Ticket | `TKT-<PHASE>-<NNN>-<slug>.md` | `TKT-P0-001-postgres-rls.md` |
| DevEx ticket | `TKT-DEVEX-<NNN>-<slug>.md` | `TKT-DEVEX-001-justfile-fix.md` |
| BLOCKER | `BLOCKER-<N>-<slug>.md` | `BLOCKER-1-pen-test-closer.md` |
| Progress log | `progress/YYYY-MM-DD-<topic>.md` | `progress/2026-09-18-bootstrap.md` |

Slugs are kebab-case, max 5 words.

## Status enum

| Status | Meaning |
|---|---|
| **Upstream (pre-burn-queue)** | |
| `DISCOVERED` | Surfaced by discovery, not yet a ticket. |
| `DRAFT` | Being written, not yet ready. |
| `READY` | Drafted, awaiting decision. |
| `AWAITING_APPROVAL` | High-risk ticket in human review queue. |
| `QUEUED` | Approved, in the burn queue. |
| **Mid-flight** | |
| `CLAIMED` | Picked up by an agent (lease acquired). |
| `EXECUTING` | Actively implementing. |
| `VERIFYING` | Verifier running. |
| **Failure** | |
| `BLOCKED` | Cannot proceed; see BLOCKER file. |
| `PARTIAL` | Substantive progress, but specific items remain. |
| **Success** | |
| `DONE` | All AC satisfied + verified + locally landed. |
| **Downstream** | |
| `SHIPPED` | Landed in a durable external record. |
| `DEPRECATED` | Superseded by a newer ticket. |
| `CANCELLED` | Decided to not pursue. |

**Allowed transitions:**

- Upstream: `DISCOVERED → DRAFT → READY → (AWAITING_APPROVAL | QUEUED) → QUEUED`
- Mid-flight: `QUEUED → CLAIMED → EXECUTING → VERIFYING → DONE`
- Downstream: `DONE → SHIPPED` or `DONE → DEPRECATED`
- Cancellation: allowed from any non-terminal state
- Recovery: `BLOCKED → QUEUED` (unblocked); `PARTIAL → EXECUTING` (resume)

**Illegal transitions:**

- `READY → EXECUTING` (must go through QUEUED → CLAIMED first)
- `DONE → QUEUED`, `SHIPPED → EXECUTING`, `DEPRECATED → QUEUED`, `CANCELLED → QUEUED`

**Two-state semantics** — `DONE` (local) vs `landed_at` (durable):

| Field | Set when | Means |
|---|---|---|
| `completed_at` | Status flips to `DONE` | All AC + 3-perspective verification passed |
| `landed_at` | Change is durable | git commit pushed, user ratified, or cloud deployed |

`DONE` may legitimately have `landed_at: null` in environments without a git remote.

## Frontmatter (ticket files)

```yaml
---
id: TKT-P0-001
title: Postgres multi-tenant schema with RLS
phase: P0
priority: P0
status: DONE
created: 2026-09-13
updated: 2026-09-19
owner: <prefix>-team
adr_refs: [the project's decision records]
prd_ref: <the project's PRD id>
wave: 5
estimated_effort: 1w
depends_on: []                       # see §Dependency declaration below
commit_ref: <sha>
landed_at: <ISO timestamp | null>

# Lease lock (set by burn-queue)
picked_up_by: <agent-id>
picked_up_at: <ISO timestamp>
lease_expires_at: <ISO timestamp>
lease_token: <int>
lease_ttl_minutes: 15

# Completion metadata (set on DONE)
completed_at: <ISO timestamp>

# Verification (3-perspective voting + loop-until-PASS)
verification:
  correctness: PASS
  per_tenant: PASS
  failure_modes: PASS
  retries: 0
  verified_at: 2026-09-19T...
  evidence: [{file, line, note}, ...]
  agent_count: 7
  pattern: [sectioning, orchestrator-workers, voting]
---
```

### Dependency declaration — `depends_on:`

Every ticket carries `depends_on:`. A ticket is claimable only if:
1. `depends_on:` is present (with entries or explicit empty list `[]`), OR
2. `phase:` is in the exempt set `{P0, P1}`.

```yaml
depends_on: [TKT-XXX-NNN, TKT-YYY-MMM]
# or
depends_on: []
```

Pre-claim check: `python3 orchestrator/scripts/dependency_audit.py <ticket_id>`

### Ticket lease lock

Lease state lives in the orchestrator's chosen state store (Postgres BIGSERIAL / SQLite / file / skip). The lease table:

| Column | Type | Purpose |
|---|---|---|
| `ticket_id` | `TEXT PRIMARY KEY` | One lease per ticket |
| `tenant_id` | `UUID` (or TEXT) | Per-tenant isolation key (RLS) |
| `holder_id` | `TEXT` | Identifies the lease holder |
| `lease_expires_at` | `TIMESTAMPTZ` | Hard expiry |
| `fencing_token` | `BIGSERIAL UNIQUE` | Monotonic — every claim/heartbeat increments it |
| `claimed_at` | `TIMESTAMPTZ` | Audit |

The adapter module (`lease.py`) exposes: `claim()`, `heartbeat()`, `release()`, `reap_stale()`, `read_lease()`. All writes go to the state store FIRST; the markdown cache is updated on success.

### Auto-derived `lease_ttl_minutes`

```
lease_ttl_minutes = max(15, ceil(estimated_effort_days * 1440 * 0.25), capped at 60)
```

Parsed from `estimated_effort` (e.g. `1w` → 5, `1d` → 1, `4h` → 0.167, `30m` → 0.021).

## Cascade marker convention

The cascade touches multiple files. To handle crashes mid-cascade, write a marker file BEFORE the cascade starts and DELETE it AFTER:

```bash
mkdir -p progress
cat > progress/.cascade-in-progress-<TKT-NNN>.json <<'EOF'
{ "ticket_id": "TKT-NNN", "started_at": "<ISO>", "step": "6.1" }
EOF
```

If the marker is present at session start, the recovery protocol reconciles.

## Cascade outbox protocol

Per TKT-ORCH-012, every cascade action is enqueued to `orchestrator.outbox` in a single transaction. The consumer applies them idempotently. Crash between enqueue and apply is safe — the row stays unapplied until the next consumer run.

## Sub-agent return schemas

When dispatching sub-agents, always pass `schema:` (JSON Schema):

```json
{
  "type": "object",
  "properties": {
    "files_changed": [{"type": "object"}],
    "tests_added": [{"type": "object"}],
    "verification": {"type": "object"}
  },
  "required": ["files_changed", "tests_added", "verification"]
}
```

## Commit message format

```
<ticket-id>: <short summary>

<paragraph: what changed and why>

<file:line evidence>

Co-Authored-By: <author-tag> <noreply@example.com>
```
