---
description: /orch-golden — run the 20-scenario regression suite. Catches failure modes of the orchestrator primitives (race conditions, verifier loopholes, RAM constraints, crash recovery, etc.).
argument-hint: "[--strict] [--json]"
---

# the orchestrator Golden — 20 Regression Scenarios

You are the **regression runner** for the orchestrator 2.0. The 20 golden scenarios (`orchestrator/scripts/golden_scenarios.py`) are scenario-based tests that exercise hard failure modes of the orchestrator. Per master directive §42.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/scripts/golden_scenarios.py` (the scenarios).
2. `<project_root>/orchestrator/tests/` (the unit tests — different scope).

## 1. Run all 20 scenarios

```bash
# Default — print PASS / WARN / FAIL summary.
python3 orchestrator/scripts/golden_scenarios

# CI mode — non-zero exit on any FAIL; with --strict, also on WARN.
python3 orchestrator/scripts/golden_scenarios --strict

# JSON output (machine-readable).
python3 orchestrator/scripts/golden_scenarios --json
```

## 2. The 20 scenarios (per master directive §42)

| # | Scenario | Status when working |
|---|---|---|
| S-001 | Two tickets modify the same file | orchestrator serializes them |
| S-002 | A ticket violates an ADR | caught by architecture_reconcile |
| S-003 | A ticket's dependencies are incomplete | caught by dependency_audit |
| S-004 | A worker crashes | lease + outbox + checkpoint recover state |
| S-005 | A verifier lies / self-attests | verifier_provenance rejects |
| S-006 | Three agents disagree | evaluator-optimizer drives fix |
| S-007 | Research sources conflict | research_ledger surfaces contradiction |
| S-008 | A UI ticket invents a duplicate component | ui_design_system expert flags |
| S-009 | domain-specific workflow assumption is unsupported | domain-specific_ops_specialist flags |
| S-010 | Architecture drift appears | architecture_reconcile catches |
| S-011 | Product requirement conflicts with architecture | vision_audit catches |
| S-012 | Machine RAM becomes constrained | scheduler throttles |
| S-013 | API rate limits are reached | (planned: explicit backoff) |
| S-014 | An implementation discovers a better feature | ticket_factory supports plan replacement |
| S-015 | A ticket is no longer necessary | discovery catches stale tickets |
| S-016 | A completed ticket creates a regression | (planned: regression suite) |
| S-017 | The orchestrator crashes during cascade | cascade marker + outbox recover |
| S-018 | An agent returns malformed structured output | schema contract at tool layer |
| S-019 | A generated ticket has no evidence | ticket_factory quality gate |
| S-020 | The project has no obvious next ticket | discovery + vision find new work |

## 3. Interpret the output

```
Golden scenarios — 20
  PASS : 11
  WARN : 7
  FAIL : 2
  SKIP : 0
```

| Status | Meaning |
|---|---|
| **PASS** | The scenario's expected behavior was observed. |
| **WARN** | The scenario's expected behavior was observed with caveats (e.g. partial coverage). Triable. |
| **SKIP** | Required fixture not present (e.g. no `db/migrations/`). Not a failure. |
| **FAIL** | The scenario's expected behavior was NOT observed. Must be triaged. |

## 4. When to run

- **Before every PR merge.** Scenario regressions are cheap (5-15s).
- **After every orchestrator-self change** (`/orch-evolve` produces orch-self tickets).
- **Nightly in CI** as a regression smoke test.
- **After upgrading any orchestrator primitive** (lease, outbox, scheduler, etc.).

## 5. Hand off

- `/orch-ticket` — convert FAIL findings into TKT-NNN tickets.
- `/orch-evolve` — when a scenario persistently WARNs, file a ticket to address.
- `/orch-status` — surfaces the latest scenario rollup.

## Date

2026-09-20.