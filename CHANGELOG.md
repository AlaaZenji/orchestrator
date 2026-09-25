# Changelog

## 0.1.0 — 2026-09-26

Initial release. Extracted from a production orchestrator that powered
ticket-driven development of a multi-tenant domain-specific platform, then scrubbed
of all project-specific content.

Includes:
- 11 runtime scripts (burn queue, lease, heartbeat, watchdog, outbox,
  outbox_consumer, ticket_state_machine, dependency_audit,
  verdict_conflict, dag_optimizer, force_claim)
- 3-doc spine (ARCHITECTURE.md, WORKFLOW.md, CONVENTIONS.md)
- Anti-stall prompt template
- 20 slash command templates (installable globally with custom prefix)
- `/setup-project` slash command (laptop-wide bootstrap)
- `orchestrator-setup` CLI (init, sync, upgrade, doctor, install-cli)
- Postgres migrations (V001 lease, V002 outbox) — opt-in RLS via GUC
- SQLite migration (V001 orchestrator tables)
- File-mode lease/outbox (best-effort, no DB)
- Skip-mode (no state layer)

Anti-stall guarantees baked in:
- WRITE-FILES-FIRST (worker writes within 3 tool calls)
- Heartbeat every 5 calls
- 30-min lease TTL with derived computation
- 3-min watchdog timeout
- No inline verifier sub-agents (saves 50% of stall risk)
- Kleppmann monotonic fencing tokens (Postgres BIGSERIAL)
