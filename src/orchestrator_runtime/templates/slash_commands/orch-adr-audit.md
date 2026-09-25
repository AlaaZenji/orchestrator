---
description: ADR audit (the project's decision records) — parse every ADR, extract the Decision section's imperative claims, and verify each claim is observed in the codebase. Emits drift findings.
argument-hint: "[--write] [--report] [--strict]"
---

# the orchestrator ADR Audit — ADR ↔ Code Compliance

You are the **ADR audit agent** for the orchestrator 2.0. The single biggest gap in the entire governance-tool landscape (per `orchestrator/docs/research/04-architecture-governance.md`) is: "is the code actually faithful to the ADR?" Every other tool stops at "ADR next to the service." Tdomain-specific script closes that gap.

## 0. Mandatory context (parallel reads)

1. `<project_root>/docs/03-architecture/adr/` (the canonical ADR corpus).
2. `<project_root>/orchestrator/world-model/adr-index.yaml` (the orchestrator's index).
3. `<project_root>/orchestrator/ARCHITECTURE.md` (the invariant summary).

## 1. Run the audit

```bash
# Default — print to stdout.
python3 orchestrator/scripts/adr_audit

# Write findings to architecture-drift.yaml + JSON report.
python3 orchestrator/scripts/adr_audit --write --report

# CI gate — non-zero exit on any FAIL.
python3 orchestrator/scripts/adr_audit --strict
```

The script enforces these checks:

| Check | Severity | Trigger |
|---|---|---|
| **ADR-indexed** | LOW/MEDIUM | Every ADR file is in `world-model/adr-index.yaml` |
| **no-mirth-connect** | CRITICAL | Implementation files (`*.java`, `*.py`, `*.ts`, `*.tsx`, `*.yaml`, `*.yml`, `*.properties`, `*.json`) referencing "Mirth Connect" |
| **no-waystar-lebanon** | HIGH | Implementation files referencing "Waystar" |
| **rls-canonical-V025+** | CRITICAL | V025+ migrations missing tenant_id / FORCE RLS / canonical NULLIF GUC pattern |
| **no-plaintext-mllp** | CRITICAL | Plaintext MLLP listener (port 6661 without TLS) |

Documentation files (`*.md`) discussing the rejection are exempt from `no-mirth-connect` / `no-waystar-lebanon` — they should explain *why* the tools are forbidden, not be forbidden themselves. The audit infrastructure itself (`scripts/adr_audit.py`, etc.) is also exempt.

## 2. Interpret the output

```
ADR audit — 33 checks (27 passed, 6 failed)
  ✗ [CRITICAL] the project's decision records no-mirth-connect: 2 implementation file(s) reference Mirth Connect
      services/onboarding/src/astra_onboarding/checklist.py
      tools/load/src/astra_load/seed.py
  ✗ [HIGH    ] the project's decision records no-waystar-lebanon: 1 implementation file(s) reference Waystar
      services/sync-gateway/app/nssf/models.py
```

Each `✗` is real drift. Triage by severity:

- **CRITICAL** — must be fixed before merge; raises P0 BLOCKER per CLAUDE.md #2.
- **HIGH** — should be fixed before next slice; raises ticket.
- **MEDIUM / LOW** — back-burner; appended to opportunities.

## 4. Hand off

- `/orch-architect` — runs the broader architecture reconciliation; ADR audit is one of its detection passes.
- `/orch-ticket` — converts HIGH / CRITICAL findings into tickets.
- `/orch-status` — surfaces the latest counts.

## Date

2026-09-20.