---
description: Fitness functions — 8 atomic CI gates, one per CLAUDE.md non-negotiable. Run before merging any change; non-zero exit on FAIL.
argument-hint: "[--gate <name>] [--strict]"
---

# the orchestrator Fitness Functions — 8 Atomic CI Gates

You are the **fitness function runner** for the orchestrator 2.0. The 8 CLAUDE.md non-negotiables are encoded as atomic fitness functions, each owned by a named CI gate. Per `orchestrator/docs/research/00-SYNTHESIS.md §4 recommendation #12`, these gates are the load-bearing discipline that ensures Orchestrator 2.0 actually ships.

## 0. Mandatory context (parallel reads)

1. `<project_root>/CLAUDE.md` (the 8 non-negotiables).
2. `<project_root>/orchestrator/scripts/fitness_functions.py` (the gates).

## 1. Run all 8 gates

```bash
# Default — print PASS / WARN / FAIL summary.
python3 orchestrator/scripts/fitness_functions

# Run a single gate by name.
python3 orchestrator/scripts/fitness_functions --gate outbox_first

# CI gate — non-zero exit on any FAIL; with --strict, also on WARN.
python3 orchestrator/scripts/fitness_functions --strict
```

The 8 gates:

| Gate | Enforces | Severity on FAIL |
|---|---|---|
| **tenant_isolation** | the project's decision records: every V025+ migration has `tenant_id` first + FORCE RLS + canonical NULLIF GUC | CRITICAL |
| **no_real_phi** | CLAUDE.md #3: no real PHI in development | CRITICAL |
| **outbox_first** | TKT-NNN / CLAUDE.md #4: operator actions go through outbox BEFORE audit | HIGH |
| **no_samd_claims** | CLAUDE.md #5 + the project's decision records: marketing copy doesn't claim diagnostic / treatment | HIGH |
| **eu_ai_act_ready** | the project's decision records: every model dir has `validation_report.md` or `model_card.yaml` | HIGH |
| **offline_first** | the project's decision records + CLAUDE.md #7: UI uses IndexedDB outbox + NetworkUnavailableError | HIGH |
| **external_validation** | CLAUDE.md #8 + V2: every model dir has external-validation report | HIGH |
| **architecture_drift** | the project's decision records: adr_audit.py reports no new CRITICAL/HIGH drift | HIGH |

## 2. Interpret the output

```
Fitness functions — 8 gates
  PASS: 5, WARN: 0, FAIL: 3

  [✓] tenant_isolation: PASS — all 3 V025+ migrations compliant
  [✓] no_real_phi: PASS — no PHI-like patterns detected in code
  [✗] outbox_first: FAIL — 1 file(s) emit audit BEFORE outbox — outbox-first violation
  [✗] no_samd_claims: FAIL — 24 marketing line(s) make SaMD-adjacent claims
  [✓] eu_ai_act_ready: PASS — no model artifacts detected (nothing to validate)
  [✓] offline_first: PASS — UI uses offline outbox + network-state check
  [✓] external_validation: PASS — no model artifacts detected (nothing to validate)
  [✗] architecture_drift: FAIL — 2 CRITICAL drift findings from adr_audit
```

Each `✗` is a real finding. Triage by gate.

## 3. When to run

- **Before every PR merge.** Tdomain-specific is the gate.
- **After every architectural change** (ADR amendment, schema migration, model retrain).
- **Nightly** in CI as a cron-driven smoke test.
- **After `/orch-architect`** — to confirm no new drift.

## 4. Hand off

- `/orch-ticket` — convert HIGH / CRITICAL failures into tickets.
- `/orch-architect` — for the `architecture_drift` gate specifically.
- `/orch-evolve` — if a gate persistently fails, file a TKT-NNN ticket to fix the gate itself.

## Date

2026-09-20.