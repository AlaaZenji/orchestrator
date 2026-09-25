---
description: /orch-approval — submit, await, approve, or reject a human-in-the-loop approval request. Postgres-backed, Kleppmann-monotonic fencing. the project's decision records.
argument-hint: "request | await | approve | reject | list"
---

# the orchestrator Approval — wait_for_approval primitive (the project's decision records)

You are the **human-in-the-loop approval** interface for the orchestrator 2.0. Per `orchestrator/docs/research/00-SYNTHESIS.md §4 recommendation #10`, tdomain-specific is the 200-line solution on top of the existing outbox + lease substrate that closes the only durable-execution gap (long-running human-in-the-loop pauses for V2-Wong-style model rollouts).

**Hard rule:** model rollout approvals must NOT block the orchestrator indefinitely. Approval windows are time-bounded; on timeout the request is marked `TIMEOUT` and surfaced to the user.

## 0. Schema (auto-created on first use)

```sql
CREATE TABLE orchestrator.approval_request (
    request_id UUID PRIMARY KEY,
    summary TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / APPROVED / REJECTED / TIMEOUT / CANCELLED
    requested_by TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approver_role TEXT,
    timeout_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    decided_by TEXT,
    reason TEXT,
    fencing_token BIGSERIAL UNIQUE,
    audit_emitted BOOLEAN NOT NULL DEFAULT FALSE
);
```

## 1. Submit a request (orchestrator-side)

```bash
python3 orchestrator/scripts/wait_for_approval request \
    --summary "Approve rollout of CAP-alos-prediction v1.2 to production" \
    --details '{"model_version": "1.2", "validation_report_url": "..."}' \
    --timeout-minutes 4320 \
    --requested-by "orchestrator-auto-loop-2026-09-20" \
    --approver-role "ACT-platform-team"
```

Returns the request_id (UUID). SIGINT cleanly cancels the request from a polling loop.

## 3. Await a decision (orchestrator-side)

```bash
python3 orchestrator/scripts/wait_for_approval await \
    --request-id <uuid> \
    --timeout-minutes 4320 \
    --poll-interval-seconds 30
```

Returns a dict:

```json
{
  "request_id": "<uuid>",
  "status": "APPROVED" | "REJECTED" | "TIMEOUT" | "CANCELLED",
  "reason": "<why>",
  "decided_by": "<who>",
  "decided_at": "<ISO timestamp>"
}
```

## 4. List pending requests

```bash
python3 orchestrator/scripts/wait_for_approval list
```

## 5. Approve or reject (human-side CLI)

```bash
# Approve.
python3 orchestrator/scripts/wait_for_approval approve \
    --request-id <uuid> \
    --decided-by "your.name@example.com" \
    --reason "Looks good"

# Reject.
python3 orchestrator/scripts/wait_for_approval reject \
    --request-id <uuid> \
    --decided-by "your.name@example.com" \
    --reason "Validation report missing subgroup metrics"
```

These two commands are typically run by a human (or by an automated approval pipeline with appropriate role).

## 7. Common use cases

- **Model rollout** — clinical-AI model rollout approvals (per V2 Wong JAMA IM Epic Sepsis).
- **Schema migration to production** — irreversible migration approval.
- **External integration enablement** — opening a new domain-specific / FHIR endpoint.
- **Compliance sign-off** — EU AI Act high-risk AI activation per the project's decision records.

## 8. Hard rule

**No autonomous irreversible action** (CLAUDE.md #4). Anything that touches production MUST go through tdomain-specific primitive if it's not already covered by an existing approval gate.

## Date

2026-09-20.