---
description: /orch-scheduler — inspect or persist the resource-aware scheduler state. Hysteresis-based 4-state machine. macOS-specific (sysctl + vm_stat). Debug utility.
argument-hint: "[--persist] [--json] [--reset]"
---

# the orchestrator Scheduler — Resource Concurrency Inspector

You are the **scheduler inspector** for the orchestrator 2.0. The scheduler is a deterministic 4-state machine (NORMAL / HIGH_LOAD / THROTTLED / RECOVERY) that controls how many sub-agents of each class (Explore / general-purpose / Plan / Workflow / Verifier) can run in parallel. Per `orchestrator/scripts/resource_scheduler.py`.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/scripts/resource_scheduler.py` (the source).
2. `<project_root>/orchestrator/STATE.md` (the existing orchestrator's cap table — for comparison).

## 1. Inspect the scheduler

```bash
# Default — print human-readable.
python3 orchestrator/scripts/resource_scheduler

# JSON output (machine-readable).
python3 orchestrator/scripts/resource_scheduler --json

# Reset to NORMAL state.
python3 orchestrator/scripts/resource_scheduler --reset

# Persist state to state/scheduler-state.json (read by /orch-auto).
python3 orchestrator/scripts/resource_scheduler --persist
```

Output (human-readable):

```
State: NORMAL
Free pages: 11,418
CPU%: 12.8
Load/cpu: 0.13
RAM used: 99.0%

Caps (max concurrent per agent class):
  explore              0 active /  6 cap
  general-purpose      0 active /  4 cap
  plan                 0 active /  1 cap
  workflow             0 active / 10 cap
  verifier             0 active /  4 cap

Peak in-flight target: 11 (Explore 6 + GP 4 + Plan 1, with ~2 GB headroom for orchestrator + OS)
```

## 2. State machine

```
NORMAL  → HIGH_LOAD     (30s sustained above HIGH_LOAD threshold)
HIGH_LOAD → THROTTLED   (60s sustained above THROTTLED threshold)
THROTTLED → RECOVERY    (15s sustained below THROTTLED threshold)
RECOVERY → NORMAL       (60s sustained below HIGH_LOAD threshold)
```

The hysteresis prevents oscillation when resources hover around a threshold.

## 3. Thresholds (tunable per `Thresholds` dataclass)

| Metric | HIGH_LOAD | THROTTLED |
|---|---|---|
| CPU% sustained | ≥ 70 | ≥ 80 |
| Free pages | < 300k | < 100k |
| Load/cpu | ≥ 1.5 | ≥ 2.0 |
| Memory pressure | warning | critical |

## 4. Hard rule

The scheduler is **NEVER** LLM-driven. It is stdlib-only Python that reads `sysctl`, `vm_stat`, `top` on macOS. The orchestrator never lets the laptop crash; if the system is in genuine danger, the scheduler pauses.

## 5. When to use

- **Debugging OOM / thrash** — see current state and active-agent counts.
- **Before / after a heavy burn** — confirm the scheduler recovers.
- **After changes to thresholds** — verify the hysteresis still works.

## 6. Hand off

- `/orch-auto` — uses `scheduler-state.json` at autonomy ≥ 1.
- `/orch-status` — surfaces the current caps in the dashboard.
- `/orch-evolve` — if the hysteresis is misbehaving, file a TKT-NNN ticket.

## Date

2026-09-20.