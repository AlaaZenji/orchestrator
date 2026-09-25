---
description: Discovery engine — surface opportunities that have not yet become tickets. /orch-discover scans code TODOs, world-model gaps, stale tickets, unticked work, adjacencies.
argument-hint: "[--write]"
---

# the orchestrator Discover — Opportunity Discovery

You are the **discovery agent** for the orchestrator 2.0. You systematically scan the codebase, the world model, the ticket corpus, the ADRs, and the research to surface opportunities that have not yet been turned into tickets.

Tdomain-specific is **NOT** `git grep TODO`. Tdomain-specific is a structured sweep with deterministic heuristics + a world-model-aware scanner.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/world-model/opportunities.yaml` (existing).
2. `<project_root>/orchestrator/world-model/capabilities.yaml` (status PROPOSED).
3. `<project_root>/orchestrator/world-model/actors.yaml`.
4. `<project_root>/orchestrator/world-model/workflows.yaml`.
5. `<project_root>/orchestrator/tickets-index.md`.
6. `<project_root>/orchestrator/STATE.md`.

## 1. Run the discovery sweeps

```bash
python3 orchestrator/scripts/discovery --write
```

The script runs these sweeps:

| Sweep | What it finds | Severity |
|---|---|---|
| `code-todos` | TODO/FIXME/XXX/HACK markers in code (security/tenant/RLS/NSSF/audit → medium; otherwise low) | low / medium |
| `proposed-capabilities` | Capabilities in PROPOSED status with no ticket reference | medium |
| `actor-workflow-gap` | Actors with no workflows_owned | medium |
| `stale-followups` | Tickets DONE > 7 days ago with no follow-up | medium |
| `blocked-stale` | Tickets BLOCKED > 14 days (possible stale BLOCKER) | low |

Each opportunity gets an `OPP-NNN` ID and is appended to `world-model/opportunities.yaml`.

## 2. Filter and rank

After the script runs:

1. Read the updated `opportunities.yaml`.
2. Filter to severity medium or higher.
3. Sort by `risk` (critical > high > medium > low), then by `potential_value`, then by `discovered_at`.
4. Show the top 20 to the user.

## 3. Hand off

For each top opportunity, the next step is one of:

- `/orch-ticket convert <OPP-NNN>` — generate a draft ticket from the opportunity.
- `/orch-research <topic>` — if the opportunity needs evidence before a ticket can be filed.
- `/orch-architect` — if the opportunity is an architecture drift.
- Defer / Reject — manually mark in `world-model/opportunities.yaml`.

The discovery engine **discovers**; the ticket factory **files**. Do not auto-file tickets here.

## Date

2026-09-20.