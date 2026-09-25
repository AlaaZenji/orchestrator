---
description: /orch-auto — the autonomous master loop. Continuous REALITY→OBSERVE→RESEARCH→UNDERSTAND→VISION→ARCHITECTURE→DISCOVER→DECIDE→GENERATE→EXECUTE→VERIFY→RECONCILE→LEARN. Stops on queue exhaustion, human-required, hard invariant, resource safety, budget, explicit stop.
argument-hint: "[--autonomy N] [--max-iterations N] [--delay SECONDS] [--dry-run]"
---

# the orchestrator Auto — Autonomous Master Loop

You are the **autonomous orchestrator** for the orchestrator 2.0. You run the continuous loop:

```
REALITY → OBSERVE → RESEARCH → UNDERSTAND → VISION →
ARCHITECTURE → DISCOVER → DECIDE → GENERATE →
DEPENDENCY-OPTIMIZE → EXECUTE → VERIFY → RECONCILE → LEARN
```

Tdomain-specific is the master command. By default it proposes tickets (autonomy level 2) and does not auto-implement. Higher autonomy levels implement with auto-pause gates.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/STATE.md`.
2. `<project_root>/orchestrator/world-model/vision.yaml`.
3. `<project_root>/orchestrator/world-model/opportunities.yaml`.
4. `<project_root>/orchestrator/progress/.last-completed-layer.txt` (resume checkpoint).
5. `<project_root>/orchestrator/tickets-index.md`.

## 1. Autonomy levels

| Level | Behavior |
|---|---|
| 0 (inspect only) | Read-only. No writes. |
| 1 (research) | Write to world-model/{product,architecture,ux,security}-drift.yaml. |
| 2 (propose tickets) | Generate DRAFT tickets via /orch-ticket (default). |
| 3 (create tickets) | Move DRAFT → READY → QUEUED. |
| 4 (implement low-risk) | Burn P2/P3 tickets via /orch-burn-queue. |
| 5 (fully autonomous) | Burn any priority. 6-lens verifier mandatory. Auto-pause gates active. |

Default: 2 (safe). Bump to 5 only after the orchestrator has demonstrated stable behavior on your repo for 5+ runs.

## 2. Run the autonomous loop

```bash
# Default: propose tickets, max 100 iterations, 5 sec delay
python3 orchestrator/scripts/auto_loop --autonomy 2 --max-iterations 100 --delay 5

# Dry run (one iteration, no writes)
python3 orchestrator/scripts/auto_loop --dry-run

# Fully autonomous (P0..P3, with auto-pause)
python3 orchestrator/scripts/auto_loop --autonomy 5
```

The script performs each iteration as:

1. **Resource scheduler tick** — read CPU / RAM / free pages; compute current caps.
2. **World model load** — every file in `world-model/` is read into memory.
3. **Vision reconciliation** — `/orch-vision` audit; emit drift findings.
4. **Architecture drift check** — `/orch-architect` reconciliation.
5. **Discover opportunities** — `/orch-discover` sweeps.
6. **Research** — (LLM-driven; outside the deterministic loop).
7. **Expert council** — `/orch-expert-council` routing.
8. **Decision** — mark opportunities ACCEPTED/REJECTED based on severity.
9. **Ticket factory** — `/orch-ticket` generate drafts.
10. **DAG optimization** — compute executable frontier.
11. **Resource allocation** — adjust caps via the scheduler.
12. **Execute** — delegate to `/orch-burn-queue` (only at autonomy ≥ 4).
13. **Verify** — 6-lens adversarial (only at autonomy ≥ 4).
14. **Cascade** — outbox-driven file updates (only at autonomy ≥ 4).
15. **World model update** — drift findings + opportunities + decisions appended.
16. **Architecture reconciliation** — re-run the architecture drift detector.
17. **Vision recheck** — has the vision drifted?
18. **New opportunities?** — if yes, continue; if no, continue until configured stopping condition.

## 3. Stopping conditions

The loop terminates on the first of:

| Condition | Action |
|---|---|
| Queue exhausted | Two consecutive iterations with zero new work. |
| Human required | P0 BLOCKER surfaced. Surface to user. |
| Hard invariant violated | CLAUDE.md #1-8 violated. STOP + raise P0 BLOCKER. |
| Resource safety | Scheduler state = THROTTLED for sustained period. |
| Systemic verification failure | More than half of in-flight tickets fail 3 retries. |
| Budget | Token budget at 95% (per project decision record). |
| Explicit stop | SIGINT, stop file, or user command. |

## 4. Auto-pause gates (the project's decision records)

These 5 triggers halt the loop:

1. CLAUDE.md violation (cross-tenant, real PHI, diagnostic claim, irreversible action).
2. Wrong foundational decision (ADR violation, slice #1 scope break).
3. Resource ceiling hit (CPU > 80% sustained, free pages < 100k, OOM kill).
4. Verification FAIL after 3 retries.
5. User-team BLOCKER (cloud account, vendor docs, NSSF data, Lebanon MOPH sign-off).

## 5. Hand off

When the loop terminates:

1. Read `state/auto-loop-{timestamp}.json` for the full run log.
2. Read `state/scheduler-state.json` for the resource profile.
3. Surface the stop reason to the user.
4. If stopped on auto-pause, file a TKT-NNN ticket to address the underlying gap.

The autonomous loop is the composition layer that orchestrates every other Orchestrator 2.0 system.

## Date

2026-09-20.