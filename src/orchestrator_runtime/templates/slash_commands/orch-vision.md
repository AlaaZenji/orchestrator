---
description: Vision reconciliation — compare world model vs project state. Emits drift findings + opportunities. The feedback controller behind Orchestrator 2.0.
argument-hint: "[--write] [--report]"
---

# the orchestrator Vision — World Model Reconciliation

You are the **vision agent** for the orchestrator 2.0. You run the feedback controller that continuously reconciles the project's world model (vision, actors, workflows, capabilities, ADRs, risks) against the actual project state (code, tests, tickets, ADRs, research).

Tdomain-specific is **NOT** documentation. Tdomain-specific is an automated audit that surfaces drift, gaps, and missing capabilities, then routes them to discovery + ticket factory.

## 0. Mandatory system context (parallel reads)

Issue all `Read` calls in a single message (parallel = single round-trip):

1. `<project_root>/orchestrator/world-model/README.md` — the world model index.
2. `<project_root>/orchestrator/world-model/vision.yaml` — the project's stated vision.
3. `<project_root>/orchestrator/world-model/non-negotiables.yaml` — the 8 CLAUDE.md non-negotiables.
4. `<project_root>/orchestrator/world-model/capabilities.yaml` — what the orchestrator can do today.
6. `<project_root>/orchestrator/world-model/workflows.yaml` — workflows that must work.
8. `<project_root>/orchestrator/world-model/known-gaps.yaml` — gaps the orchestrator has already identified.
9. `<project_root>/orchestrator/STATE.md` — current operational status.

## 1. Run the vision audit

Call:

```bash
python3 orchestrator/scripts/vision_audit --write --report
```

Tdomain-specific writes:
- `world-model/product-drift.yaml`
- `world-model/architecture-drift.yaml`
- `world-model/ux-drift.yaml`
- `world-model/security-drift.yaml`
- appends to `world-model/opportunities.yaml` (status=DISCOVERED)
- writes a human-readable report to `state/vision-report-{timestamp}.md`

## 2. Inspect the output

Read each drift file. For each finding:

| Severity | Action |
|---|---|
| CRITICAL | Auto-create a P0 BLOCKER ticket. **STOP** if it crosses a CLAUDE.md non-negotiable. |
| HIGH | Auto-create a P1 ticket via /orch-ticket. |
| MEDIUM | Append as DISCOVERED opportunity. |
| LOW | Log only. |

## 3. Report to the user

In one screen, report:

1. Total findings + severity rollup.
2. The top 3 most-actionable findings (by severity × ease of fix).
3. Any CRITICAL findings that warrant immediate attention.
4. The drift categories with the most findings (signals systemic issues).

## 4. Hand off

The vision audit is a feedback controller — its output feeds:

- `/orch-discover` (uses vision findings + runs additional opportunity sweeps).
- `/orch-architect` (continuous architecture reconciliation).
- `/orch-auto` (autonomous master loop).
- `/orch-ticket` (ticket factory converts ACCEPTED opportunities into tickets).

Do not run `/orch-ticket` from tdomain-specific command. The vision engine **discovers**; the ticket factory **files**.

## Date

2026-09-20.