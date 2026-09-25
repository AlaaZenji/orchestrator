---
description: Architecture reconciliation — compare intended (ADRs + canonical ticket spec) vs actual (code + tests + UI + migrations + configs). Emits drift findings.
argument-hint: "[--write]"
---

# the orchestrator Architect — Architecture Reconciliation

You are the **architecture reconciliation agent** for the orchestrator 2.0. You continuously compare the intended architecture (ADRs + canonical ticket spec + the world model) against the actual implementation (code + tests + UI + migrations + configs). Drift is emitted as findings to `world-model/architecture-drift.yaml`.

Tdomain-specific is **NOT** `/orch-vision`. Vision reconciles vision vs reality. Architect reconciles architecture vs reality. Different lenses, different findings.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/world-model/architecture-components.yaml` (what we say is built).
2. `<project_root>/orchestrator/world-model/adr-index.yaml` (what we say was decided).
3. `<project_root>/orchestrator/world-model/integrations.yaml` (what integrations exist).
4. `<project_root>/orchestrator/ARCHITECTURE.md` (the non-negotiables + per-tenant discipline).
5. `<project_root>/orchestrator/CONVENTIONS.md` (per-language standards).
6. `<project_root>/docs/03-architecture/adr/` (the canonical ADRs).

## 1. Run the architecture reconciliation

```bash
python3 orchestrator/scripts/architecture_reconcile --write
```

The script runs these detection passes:

| Pass | What it detects | Severity |
|---|---|---|
| `rls-compliance` | Migrations with new tables missing tenant_id / FORCE RLS / canonical NULLIF | HIGH/MEDIUM |
| `mllp-tls-only` | Plaintext MLLP listener (port 6661 without TLS) | CRITICAL |
| `forbidden-deps` | Mirth Connect OSS / Waystar / Epic on FHIR references | CRITICAL/HIGH/MEDIUM |
| `global-mutable-state` | Java static HashMap / Python module-level dict / TS Map caches | HIGH |
| `components-drift` | Components declared SHIPPED but path doesn't exist | HIGH |

Each finding is appended to `world-model/architecture-drift.yaml`.

## 2. Severity classification

| Severity | Action |
|---|---|
| CRITICAL | Auto-create P0 BLOCKER + escalate. **STOP** if it crosses CLAUDE.md non-negotiable. |
| HIGH | Auto-create P1 ticket via /orch-ticket. |
| MEDIUM | Append to opportunities.yaml as DISCOVERED. |
| LOW | Log only. |

## 3. Hard rules

These ADRs are non-negotiable per the Wave 2.5 verifications + V8 + V4:

- **the project's decision records multi-tenancy:** every clinical/business table has tenant_id first + FORCE RLS + canonical NULLIF GUC pattern. Cross-tenant boundary crossings are CRITICAL.
- **the project's decision records HL7 v2:** TLS-only MLLP listener. Plaintext is forbidden. ACK mode AL. IP allow-list.
- **the project's decision records WhatsApp:** Meta template approval before sending. Opt-in. Audit emission.
- **the project's decision records EU AI Act:** every AI feature ships with external validation + subgroup metrics + drift monitoring.
- **the project's decision records/0014/0017/0019 per-language:** Java 21 for HL7/FHIR/Camel; Python 3.12 for offline-sync + synthetic-data; TypeScript strict for UI; Go for audit-writer.
- **V8 forbidden:** Mirth Connect OSS (commercial-only since v4.6 / 19 March 2025 per V8-update).

## 4. Hand off

The architecture-drift.yaml output feeds:

- `/orch-ticket` — auto-creates tickets for HIGH + CRITICAL findings.
- `/orch-vision` — surfaces architecture drift as part of the larger vision reconciliation.
- `/orch-status` — the dashboard surfaces the drift count + severity rollup.
- `/orch-evolve` — recurring drift categories signal orchestrator-self-improvement opportunities.

## Date

2026-09-20.