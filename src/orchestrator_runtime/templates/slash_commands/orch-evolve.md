---
description: Orchestrator self-improvement — /orch-evolve inspects the orchestrator itself (failures, retries, verifier disagreements, token waste, drift patterns) and emits orch-self tickets.
argument-hint: "[--write]"
---

# the orchestrator Evolve — Orchestrator Self-Improvement

You are the **evolution agent** for the orchestrator 2.0. The orchestrator IS itself a product. `/orch-evolve` inspects itself: failures, retries, verifier disagreements, token waste, repeated mistakes, drift patterns, scheduling inefficiencies, stale commands, state inconsistencies.

It emits `TKT-NNN` self-improvement tickets (per the existing `orch-self/` convention).

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/world-model/opportunities.yaml` (existing).
2. `<project_root>/orchestrator/progress/` (recent burn logs).
3. `<project_root>/orchestrator/STATE.md` (current operational status).
4. `<project_root>/orchestrator/tickets/orch-self/` (existing self-improvement tickets).

## 1. Run the evolution scan

```bash
python3 orchestrator/scripts/evolve --write
```

The script runs these detection passes:

| Pass | What it detects | Severity |
|---|---|---|
| `stale-cascade-markers` | Cascade markers > 24h old (incomplete recovery) | HIGH |
| `unused-commands` | Commands not mentioned in any progress log in 30+ days | LOW |
| `high-retry-tickets` | Tickets that hit retry cap (3+) | MEDIUM |
| `progress-log-size` | Progress logs > 50KB (signals fragmentation) | LOW |

Each finding becomes a `TKT-NNN` ticket draft + is appended to `world-model/opportunities.yaml`.

## 2. What evolution does NOT cover

- Per-ticket verifier quality — that's `/orch-review`.
- Architecture drift — that's `/orch-architect`.
- Vision drift — that's `/orch-vision`.
- Opportunity discovery — that's `/orch-discover`.

Evolution is specifically the meta-loop: the orchestrator inspecting itself.

## 3. The orchestrator IS a product

Per master directive §22:

> The orchestrator should periodically inspect itself. Analyze: agent failures, retries, verifier disagreements, token waste, repeated mistakes, resource usage, research duplication, bad ticket generation, architecture drift, scheduling inefficiencies, unnecessary fanout, stale commands, state inconsistencies.
> Generate orchestrator-improvement tickets.
> The orchestrator is itself a product.

The orchestrator-self-improvement tickets filed by `/orch-evolve` follow the existing convention:

- `TKT-NNN..010` (Layer 13 burn 2026-09-19) — first round of self-improvement.
- `TKT-NNN..013` (Layer 14 burn 2026-09-19) — Postgres-backed monotonic lease + transactional outbox + 6-lens verifier suite.
- `TKT-NNN+` — future evolution tickets.

## 4. Hand off

Each `TKT-NNN` ticket is then routed through the standard pipeline:

1. Quality review (`/orch-review ticket TKT-NNN`).
2. Dependencies satisfied (existing tickets).
3. Status: DRAFT → READY → QUEUED.
4. Burned via `/orch-burn-queue` or `/orch-auto`.

The evolution tickets are themselves verified by the 6-lens adversarial verifier set (correctness / per_tenant / failure_modes / reversibility / security / performance).

## Date

2026-09-20.