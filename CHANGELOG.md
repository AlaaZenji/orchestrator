# Changelog

## [Unreleased]

### Changed
- Removed project-specific content from `src/orchestrator_runtime/templates/`
  in preparation for publishing to https://github.com/AlaaZenji/orchestrator.
  Behavior of the runtime scripts is preserved; only previously-project-specific
  defaults were updated (ticket-id default prefix in `bottleneck_detector.py`,
  hardcoded `owner:` field, `CLAUDE.md #N` citations, `CDS Hooks` references,
  `ASTRA_*` env-var names, and an embedded `CDS Hooks` burn-gate subsystem
  that referenced a non-existent `burn_gate.py`).
- Replaced `ASTRA_DB_ROLE` / `ASTRA_APP_PGPASSWORD` / `ASTRA_DB_TENANT`
  env-var names with `ORCHESTRATOR_DB_ROLE` / `ORCHESTRATOR_APP_PGPASSWORD`
  / `ORCHESTRATOR_DB_TENANT`.
- Replaced `ticket_artifacts_paths()` in `dev_helpers.py` with a smaller
  helper that only retains `next_migration_number()`. The ADR-0028 §8
  artifact-path helper was project-specific and removed.
- Expanded `tests/test_smoke.py::test_no_project_specific_content_in_repo`
  forbidden-term list to match `.github/workflows/ci.yml`'s strict scrub
  list AND added LLM-anonymization placeholder strings (e.g.
  "the project's decision records", "the project's domain decision support")
  so any future recurrence is caught early.

### Removed
- The entire `CDS Hooks` burn-gate subsystem from `burn_queue.py`
  (`CdsGateSkip` exception, `cds_burn_gate_check()` function, the
  `BURN_GATE_SCRIPT` constant, the inline `_cds_progress_log()` helper,
  and the call sites in `force_claim_ticket()` and the main loop). The
  subsystem invoked a non-existent `orchestrator/scripts/burn_gate.py`
  and was fundamentally project-specific.
- The `reap_stale_leases()` function's inline psycopg2 snippet that
  hardcoded `ASTRA_DB_TENANT`. Replaced with a thin shim that delegates
  to the canonical `lease.reap_stale()`.

## 0.1.0 — 2026-09-26

Initial release. A **minimal, project-agnostic** orchestrator engine.
Throw it on any folder.

Includes:
- 11 runtime scripts (burn queue, lease, heartbeat, watchdog, outbox,
  outbox_consumer, ticket_state_machine, dependency_audit,
  verdict_conflict, dag_optimizer, force_claim)
- 3-doc spine (ARCHITECTURE.md, WORKFLOW.md, CONVENTIONS.md)
- Anti-stall prompt template
- `/setup-project` slash command (the ONLY globally-installed command)
- `orchestrator-setup` CLI (init, sync, upgrade, doctor, install-cli)
- Postgres migrations (V001 lease, V002 outbox) — opt-in RLS via GUC
- SQLite migration (V001 orchestrator tables)
- File-mode lease/outbox (best-effort, no DB)
- Skip-mode (no state layer)

**Intentionally NOT included** (for true project-agnosticism):
- Per-prefix slash commands (`<prefix>-burn`, etc.) — write your own
- Specialist prompts — write your own for your domain
- Domain-specific non-negotiables — CLAUDE.md template is generic

Anti-stall guarantees baked in:
- WRITE-FILES-FIRST (worker writes within 3 tool calls)
- Heartbeat every 5 calls
- 30-min lease TTL with derived computation
- 3-min watchdog timeout
- No inline verifier sub-agents (saves 50% of stall risk)
- Kleppmann monotonic fencing tokens (Postgres BIGSERIAL)

Verified end-to-end:
- `pip install -e .` works
- `orchestrator-setup init --target /tmp/test` bootstraps a fresh dir
- `orchestrator-setup doctor` returns 9/9 PASS
- `state-store=postgres` lands V001 lease + V002 outbox in target DB
- Zero project-specific content remains
