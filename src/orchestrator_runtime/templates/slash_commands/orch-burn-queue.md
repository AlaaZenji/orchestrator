---
description: Burn 3 the orchestrator tickets in parallel, DAG-aware, lease-locked. No probes, no auto-detect, no resource gates. Per CLAUDE.md non-negotiables + Wave 2.5 verification cadence.
argument-hint: "[--dry-run]"
---

# the orchestrator Burn Queue — Cross-Ticket Parallel Scheduler

You are the **orchestrator agent** for the orchestrator. You are running the **cross-ticket queue burn** — multiple tickets worked in parallel, DAG-aware, lease-locked, with max safe sub-agent fan-out per ticket.

Tdomain-specific is **L1 parallelism** (multiple tickets in flight at once). The **L2** patterns (per-ticket sub-agent fan-out: sectioning + orchestrator-workers + voting + evaluator-optimizer) still apply **within** each ticket-worker sub-agent.

**The orchestrator burns 3 tickets in parallel. No flags, no probes, no resource gates.** (User directive 2026-09-19.)

---

## 0. Mandatory context (parallel reads)

Issue all `Read` calls in one message:

1. `<project_root>/CLAUDE.md` — non-negotiables, ADRs, verifications.
2. `<project_root>/orchestrator/README.md`.
3. `<project_root>/orchestrator/STATE.md`.
4. `<project_root>/orchestrator/ARCHITECTURE.md`.
5. `<project_root>/orchestrator/WORKFLOW.md`.
6. `<project_root>/orchestrator/CONVENTIONS.md` (lease protocol + sub-agent schemas).
7. `<project_root>/orchestrator/NEXT-ACTIONS.md`.
8. `<project_root>/orchestrator/tickets-index.md`.

If `$ARGUMENTS` mentions a BLOCKER, read `blockers/BLOCKER-*.md` in the same batch.

## 1. Parallelism

**Burn 3 tickets in parallel. Period.** (User directive 2026-09-19.)

### 1.1 max_n = 3

`max_n` is fixed at **3**. No probe, no auto-detect, no hard stops, no resource gates. If the DAG layer has fewer than 3 tickets, run what's available. If it has more than 3, the orchestrator dispatches 3 and queues the rest for the next pass.

### 1.2 Per-ticket sub-agent fan-out: max safe, never crash

Per ticket, fan out **as many sub-agents as the laptop can sustain without crashing**. Push hard, but back off before the laptop dies.

**How:**
- No artificial ceiling on fan-out — let the system run as wide as it can.
- **Crash guard (reactive, not preemptive):** monitor for precursor signals — swap thrashing, sustained load1 >> cpu, OOM-killer warnings in `dmesg`/Console, sub-agent crashes/timeouts. If precursors fire, **auto-pause the burn immediately**, surface to user, and wait for guidance.
- Do NOT preemptively throttle. The whole point is to use every available cycle.

**Verifiers (Wave 2.5, TKT-NNN):** dispatched in parallel per ticket priority — **6 for P0/P1** (correctness / per-tenant / failure-modes / reversibility / security / performance), **3 for P2/P3** (correctness / per-tenant / failure-modes); idempotency is opt-in per ticket via `idempotency_required: true` frontmatter flag. Project discipline, always run, never skip.

**Hard rule:** if the laptop is in genuine danger of crashing (kernel panic imminent, OOM kill about to fire), the orchestrator pauses — never lets it crash.

### 1.3 Parse `$ARGUMENTS` (optional)

| Args | Effect |
|---|---|
| (none) | Burn. |
| `--dry-run` | Stop after §3. Print DAG, chosen layer, lease status. Do not work. |

## 2. Build the DAG (dependency-aware queue)

0. **Resume-from-checkpoint (TKT-NNN):** Read `orchestrator/progress/.last-completed-layer.txt` (if exists). Start the DAG from `last_layer + 1`. If the file is missing or contains the initial-state sentinel `0 2026-09-19T00:00:00Z none`, start from layer 0. Programmatic read: `python3 -c 'from orchestrator.scripts.checkpoint import read_checkpoint; print(read_checkpoint())'`.
1. Read `orchestrator/NEXT-ACTIONS.md` — get priority order.
2. Read `orchestrator/tickets-index.md` — get all tickets + statuses.
3. Read each QUEUED / PARTIAL ticket's frontmatter (parallel Reads, one per ticket). Look for explicit `depends_on` field. **If absent, infer from slice phase ordering: P0 → P1 → P2 → P3 → P-DEVX.**
4. Build a layered graph:
   - **Layer 0** = tickets with no dependencies AND not held by another agent.
   - **Layer N** = tickets whose dependencies are all `DONE` (or in earlier layers AND `DONE`).
5. Filter out tickets with `status: BLOCKED`, `DONE`, `CANCELLED`.
6. Filter out tickets whose deps aren't met.

Output: an ordered list of layers, each layer containing the parallelizable tickets.

## 3. Pick the layer

Pick the **lowest-numbered layer** with ≥ 1 available ticket. Dispatch up to **3** ticket-workers from that layer in parallel.

If `--dry-run`: stop here, print:
- The DAG (per layer: which tickets, why).
- The chosen layer.
- The lease state for each ticket in the chosen layer.
- Estimated total time (heuristic: 2 days/ticket × layers).

## 4. Atomic lease claims (parallel attempts, failure-isolated)

For each ticket in the chosen layer, attempt atomic claim per `CONVENTIONS.md §Ticket lease lock`. Run these attempts **in parallel** (one Edit per ticket). On contention:

- **Claim succeeded** → add to worker dispatch list.
- **Claim failed** (another agent holds it / stale-but-not-reapable) → log to `progress/YYYY-MM-DD-burn-queue.md`, skip that ticket, try the next available ticket in the layer (if any).
- **Stale lease reaped** → log the reaper event, add to worker dispatch list.

Best-effort parallelism: if you aimed for N but only got M ≤ N claims, run M workers. Don't fail the whole burn.

## 5. Dispatch ticket-workers (parallel)

For each successfully-claimed ticket, dispatch **one `general-purpose` sub-agent** (the ticket-worker) with tdomain-specific spec:

### 5.1 Ticket-worker prompt shape

```
You are a ticket-worker for the orchestrator. You have ONE ticket assigned: <path>.

Lease info (passed by the orchestrator):
  picked_up_by: <agent-id>
  lease_expires_at: <ISO>
  lease_token: <fencing_token:int>     # BIGSERIAL from lease.py claim() (TKT-NNN)
  lease_ttl_minutes: 15

Your per-ticket sub-agent caps (MUST dispatch — see AMEND-2 in TKT-NNN):
  Explore: <k1>
  general-purpose (impl workers): <k2>
  Plan: <k3>
  verifiers (general-purpose, Wave 2.5): 6 for P0/P1, 3 for P2/P3  ← MANDATORY.
                                          priority: P0 | P1 → 6 distinct verifiers (correctness / per_tenant / failure_modes / reversibility / security / performance) — TKT-NNN.
                                          priority: P2 | P3 → 3 distinct verifiers (correctness / per_tenant / failure_modes) — security + performance + reversibility are priority-gated.
                                          idempotency is OPT-IN per ticket (add as 7th / 4th verifier if frontmatter carries `idempotency_required: true`).
                                          Self-attestation is FORBIDDEN.
                                          The orchestrator will REJECT WORKER_RESULT without verifier_provenance.

Your job (run WORKFLOW.md §3 → §6 for tdomain-specific ONE ticket):
  §3 Discovery — fan out to up to <k1> Explore agents in parallel, by concern.
  §4 Implementation — 0/1 Plan + up to <k2> general-purpose workers in parallel.
  §5 Verification — dispatch verifiers in parallel per ticket priority:
                    P0 | P1 → 6 (correctness / per-tenant / failure-modes / reversibility / security / performance).
                    P2 | P3 → 3 (correctness / per-tenant / failure-modes).
                    If idempotency_required: true → +1 (7th / 4th lens).
                  Each MUST be a distinct sub-agent (no self-attestation).
                  Capture each verifier's sub-agent ID in `verifier_provenance.verdict_sources.<lens>_agent`.
                  Loop until ALL dispatched lenses PASS, retry cap 3.
  §6 Cascade update — enqueue 5+ sub-actions to the outbox (TKT-NNN), then run the consumer, then commit.

Heartbeat: extend lease_expires_at + rotate fencing_token via lease.py heartbeat() on every commit
           OR every 10 minutes, whichever is sooner.

Return: WORKER_RESULT schema (CONVENTIONS.md §Sub-agent return schemas):
  {
    files_changed: [{path, summary}],
    tests_added: [{path, test_name}],
    verification: {
      correctness: PASS,
      per_tenant: PASS,
      failure_modes: PASS,
      reversibility: PASS,         # P0/P1 only — omit for P2/P3
      security: PASS,              # TKT-NNN — P0/P1 only — omit for P2/P3
      performance: PASS,           # TKT-NNN — P0/P1 only — omit for P2/P3
      # idempotency: PASS,         # TKT-NNN — opt-in, only if idempotency_required: true
      retries: N,
      evidence: [...],
      commit_ref: <sha>,
      verdict_sources: {
        correctness_agent: <sub-agent-id>,
        per_tenant_agent: <sub-agent-id>,
        failure_modes_agent: <sub-agent-id>,
        reversibility_agent: <sub-agent-id>,    # P0/P1 only — omit for P2/P3
        security_agent: <sub-agent-id>,         # TKT-NNN — P0/P1 only
        performance_agent: <sub-agent-id>,      # TKT-NNN — P0/P1 only
        # idempotency_agent: <sub-agent-id>     # TKT-NNN — opt-in
      }
    },
    verifier_provenance: {
      dispatched_count: 3 | 4 | 6 | 7,   # 3 for P2/P3, 4 for P2/P3-with-idempotency, 6 for P0/P1, 7 for P0/P1-with-idempotency
      sub_agent_ids: [<id1>, <id2>, <id3>]                                  # P2/P3
      # or: [<id1>, <id2>, <id3>, <id4>]                                    # P2/P3-with-idempotency
      # or: [<id1>, <id2>, <id3>, <id4>, <id5>, <id6>]                      # P0/P1
      # or: [<id1>, <id2>, <id3>, <id4>, <id5>, <id6>, <id7>]              # P0/P1-with-idempotency
    },
    outbox_audit: {
      enqueued: [<idempotency_key>, ...],          # TKT-NNN
      applied: [<outbox_id>, ...],                 # TKT-NNN
      pending: [<outbox_id>, ...]                  # TKT-NNN — should be [] before DONE
    },
    blockers_discovered: [{ticket_id, reason}],   // optional
    final_status: DONE | PARTIAL | BLOCKED
  }
```

### 5.2 Dispatch pattern

Use `parallel([w1, w2, ..., wN])` where each `wi` is a thunk invoking the ticket-worker agent. **Tdomain-specific is the `parallel` barrier pattern** (not pipeline) because we need all workers' results before the cascade update.

### 5.3 Per-worker isolation

If two ticket-workers would edit the **same file** (rare but possible if they share a config), they must NOT run truly concurrently. Either:
- Sequence those two workers (drop one from the parallel batch, run after).
- OR use `isolation: "worktree"` on the conflicting worker.

Pre-flight check: compare ticket AC scopes; flag overlap. If overlap detected, downgrade to sequential for those two.

## 6. Wait + collect + failure isolation

Wait for all ticket-workers to return. Process results:

- **Provenance check (mandatory — see AMEND-3 in TKT-NNN):** For each WORKER_RESULT, validate:
  - `verifier_provenance.dispatched_count >= 3` (minimum; 4 with opt-in idempotency, 6 for P0/P1, 7 for P0/P1-with-idempotency — see TKT-NNN)
  - `verifier_provenance.sub_agent_ids` contains `dispatched_count` distinct IDs
  - `verification.verdict_sources.correctness_agent`, `per_tenant_agent`, `failure_modes_agent` are all distinct
  - For P0/P1: also `reversibility_agent`, `security_agent`, `performance_agent` (6 distinct)
  - For opt-in idempotency: also `idempotency_agent` (+1)
  - Each `verdict_sources.*_agent` matches a sub-agent ID in `verifier_provenance.sub_agent_ids`
  - **If validation FAILS: REJECT the WORKER_RESULT.** The ticket stays at its pre-worker status. Surface the rejection in the cascade log as `[VERIFIER_PROVENANCE_REJECTED]`. The orchestrator MAY then independently dispatch the missing verifiers as a fallback (same pattern Layer 8 used on 2026-09-19 — caught 5 invariants the workers' self-attestation missed).
- **DONE with verification PASS** → add to layer-cascade update.
- **DONE with verification FAIL after 3 retries** → auto-pause trigger #5. STOP the burn. Surface to user with the failing perspective + fix_suggestion.
- **PARTIAL** → add to cascade, mark in tickets-index as 🟡 PARTIAL, continue with other tickets in the layer.
- **BLOCKED** → add to cascade, mark in tickets-index as ⚠️ BLOCKED, continue.
- **Worker crash / API error** → log, mark ticket as still IN_PROGRESS (lease held), continue with others. The lease will naturally expire and another burn can re-claim it.

**Failure isolation rule:** one ticket's failure does NOT block the other tickets in the layer. The burn proceeds with the survivors.

## 7. Cascade update (one batch per layer)

In one tool batch:

1. For each ticket in the layer, update ticket frontmatter to final state (`DONE` / `PARTIAL` / `BLOCKED`) per the WORKER_RESULT.
2. Update `tickets-index.md` — row for each ticket in the layer.
3. Append to `progress/YYYY-MM-DD-burn-queue.md` — what shipped, what was tried, evidence pointers, layer summary.
4. Update `STATE.md` ONLY if at-a-glance counts changed.
5. **One commit per layer** per `CONVENTIONS.md §Commit message format` (enhanced), e.g.:

```
[burn-queue layer N] <N> tickets shipped GREEN

- TKT-NNN: satisfied at <file:line>
- TKT-NNN: satisfied at <file:line>
- 3-perspective verification: correctness ✓ / per-tenant ✓ / failure-modes ✓ (retries: <N>)
- Sub-agent fan-out: <N ticket-workers> × <per-ticket caps> — peak <N> concurrent across tickets

Co-Authored-By: Claude Code <noreply@anthropic.com>
```

## 8. Move to next layer

Repeat from §2 (DAG may have changed if any tickets in the layer were BLOCKED). Continue until:

- **Queue empty** (all QUEUED/PARTIAL tickets are now DONE/BLOCKED/CANCELLED) → done.
- **User-team BLOCKER** fires (cloud account, vendor docs, etc.) → STOP, surface to user.
- **Budget exhausted** (token budget at 95% per the project's decision records) → STOP, surface to user.
- **Auto-pause trigger fires** → STOP, surface per §9.

## 9. Auto-pauses (5 triggers; L1-specific extension)

Same 5 as `/orch-work` (see `WORKFLOW.md §7`):

1. CLAUDE.md violation.
2. Wrong foundational decision.
3. Resource ceiling hit.
4. Verification FAIL after 3 retries (any ticket).
5. User-team BLOCKER.

**L1-specific extension — layer-failure auto-pause:** if **more than half** of the tickets in a layer fail 3 retries, pause + surface to user. Don't silently continue. Tdomain-specific guards against systemic issues (e.g., a shared config break that affects all tickets in the layer).

## 10. Honesty discipline (V1 / V3 / V4 / V7)

- Never claim a ticket is DONE without `commit_ref` + 3/3 verifier PASS + evidence.
- Never claim "layer passed" without each ticket's `WORKER_RESULT.verification` PASS.
- If the burn is partial, say so explicitly: "Layer 3: 2/3 DONE, 1 BLOCKED on TKT-NNN."
- If a ticket fails, surface the failure + `fix_suggestion` from the verifier.
- If you don't know whether a dependency is met, read the dep ticket's status — don't guess.
- Never fabricate progress. If a worker crashed, say "worker crashed — ticket still IN_PROGRESS."

## 11. First-run safety

For the **first burn** (or any burn after the orchestrator changes), use `--dry-run` first. Confirm:
- The DAG layer choice looks right.
- The lease claims succeed (no contention).
- Estimated time is reasonable.

Then re-run without `--dry-run`.

## Date

2026-09-19.