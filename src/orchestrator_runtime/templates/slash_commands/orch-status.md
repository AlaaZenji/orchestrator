---
description: /orch-status — concise operational dashboard: vision, queue, execution, resources, risks, drift, recent decisions, next autonomous action.
argument-hint: ""
---

# the orchestrator Status — Operational Dashboard

You are the **status agent** for the orchestrator 2.0. You produce a one-screen dashboard answering the question:

> **What is the orchestrator doing, why is it doing it, what is wrong, and what will happen next?**

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/STATE.md` (operational status).
2. `<project_root>/orchestrator/world-model/vision.yaml`.
3. `<project_root>/orchestrator/progress/metrics.md` (DORA-style metrics).
4. `<project_root>/orchestrator/tickets-index.md`.
5. `<project_root>/orchestrator/blockers-index.md`.
6. `<project_root>/orchestrator/world-model/opportunities.yaml`.
7. `<project_root>/orchestrator/world-model/product-drift.yaml` + `architecture-drift.yaml`.

## 1. Output structure

The dashboard has these sections (concise — one screen):

```
# the orchestrator Status — <timestamp>

## VISION
Mission: <one-line>
Slice #1: Bed Management + Command Center only (the project's decision records).
Positioning: Operations intelligence, not medical device (the project's decision records).

## ARCHITECTURE HEALTH
| Metric | Value |
|--------|-------|
| ADRs accepted | 22 |
| ADRs superseded | 0 |
| Architecture drift findings | <count from architecture-drift.yaml> |
| Components SHIPPED | <count> |
| Components PROPOSED | <count> |
| Critical drift | <count> |

## PRODUCT HEALTH
| Metric | Value |
|--------|-------|
| Vision goals | <count from goals> |
| Workflows defined | <count from workflows.yaml> |
| Capabilities SHIPPED | <count> |
| Capabilities PROPOSED | <count> |
| Actors with workflows | <count> |
| Open opportunities | <count> |
| Critical opportunities | <count> |

## QUEUE
| Status | Count |
|--------|-------|
| QUEUED | <count> |
| IN_PROGRESS | <count> |
| BLOCKED | <count> |
| PARTIAL | <count> |
| DONE (last 24h) | <count> |
| DONE (last 7d) | <count> |

## EXECUTION
| Metric | Value |
|--------|-------|
| Active burn | <name + started_at> |
| Last burn layer | <N> |
| Active leases | <count> |
| Outbox pending | <count> |
| Outbox applied/min | <count> |

## ACTIVE AGENTS
| Agent class | Active | Cap | Notes |
|-------------|--------|-----|-------|
| Explore | <n> | <n> | — |
| general-purpose | <n> | <n> | — |
| Plan | <n> | 1 | — |
| Workflow | <n> | 10 | — |
| Verifier | <n> | <n> | — |

## RESOURCE USAGE
<from `python3 orchestrator/scripts/resource_scheduler`>
| Metric | Value |
|--------|-------|
| Scheduler state | <NORMAL/HIGH_LOAD/THROTTLED/RECOVERY> |
| Free pages | <count> |
| CPU% | <pct> |
| RAM used% | <pct> |
| Load/cpu | <n> |

## BLOCKERS
<from blockers-index.md>
| # | Name | Status |
|---|------|--------|
| 1 | Pen-test P0 closer | <status> |
| 2 | TKT-NNN staging-region load test | <status> |
| 3 | RK-0005 pilot authorization | ✅ CLOSED |
| 4 | NSSF runtime claim adapter | <status> |

## RISKS
<top 5 active risks from world-model/risks.yaml>
- <risk-id>: <name> (<severity>)

## DRIFT
| Category | Count | Severity |
|----------|-------|----------|
| product | <count> | <highest> |
| architecture | <count> | <highest> |
| ux | <count> | <highest> |
| security | <count> | <highest> |

## RECENT DECISIONS
<last 5 from world-model/decisions.yaml>

## NEXT AUTONOMOUS ACTION
<one-line: what /orch-auto would do next if invoked>
```

## 2. Concision discipline

The dashboard must fit on ONE screen. If a section grows beyond 10 lines, defer to a sub-report (link to a file).

## 3. Update cadence

`/orch-status` is read-only. It does NOT modify any file.

The autonomous loop in `auto_loop.py` invokes `/orch-status` every iteration as part of its observability layer.

## Date

2026-09-20.