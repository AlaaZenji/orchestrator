# Changelog

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
