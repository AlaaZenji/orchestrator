# Capability Migration Matrix

This matrix maps every existing subsystem, script, and feature from the legacy `AlaaZenji/orchestrator` repository into its target architectural layer in the rebuilt platform.

| Existing Subsystem / Script | Legacy Location | Target Classification | Target Architectural Destination | Migration Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **Ticket State Machine** | `templates/scripts/ticket_state_machine.py`, `state.py` | **CORE** | `src/orchestration/state_machine/` | Formalized into a deterministic finite state machine with explicit legal transitions, invariants, and typed actors. |
| **Lease & Fencing Token** | `templates/scripts/lease.py`, `V001__lease.sql` | **CORE** | `src/reliability/leases/` | Purged clinical domain leaks; formalized Kleppmann-correct monotonic fencing sequences for both SQLite and Postgres. |
| **Transactional Outbox** | `templates/scripts/outbox.py`, `outbox_consumer.py` | **CORE** | `src/events/outbox/` | Re-architected to emit CNCF CloudEvents 1.0 JSON envelopes with atomic ACID database transaction coupling. |
| **Dependency Resolution** | `templates/scripts/dag_optimizer.py`, `dependency_audit.py` | **CORE** | `src/orchestration/dependency/` | Moved from ad-hoc markdown regex parsing to a topological DAG resolver operating on database entities. |
| **Scheduler & Burn Queue**| `templates/scripts/burn_queue.py`, `burnqueue_all.py` | **CORE** | `src/orchestration/scheduler/` | Replaced shell-script loops with a deterministic priority fair-share scheduler using `SKIP LOCKED` claims. |
| **Stall Watchdog** | `templates/scripts/watchdog.py`, `watchdog_loop.py` | **CORE** | `src/reliability/watchdog/` | Consolidated into a single deterministic background reconciliation loop checking lease and heartbeat timeouts. |
| **Idempotency Engine** | (Implicit in outbox) | **CORE** | `src/reliability/idempotency/` | Formalized explicit idempotency key tracking for work creation, claims, and state mutations. |
| **Verification Gate** | `templates/scripts/verifier_doctrine_gate.py` | **CORE + PLUGIN** | `src/domain/verification/` & `src/verification/` | Decoupled verification into first-class `VerificationPlan`, `VerificationRun`, and pluggable deterministic graders. |
| **Claude Agent Runner** | `templates/scripts/dispatch.py` | **ADAPTER** | `src/runtime/adapters/claude.py` | Extracted Claude Code CLI execution into a clean `AgentRuntime` provider adapter. |
| **Subprocess / Script Runner**| `templates/scripts/dispatch.py` | **ADAPTER** | `src/runtime/adapters/subprocess.py` | Standard local execution adapter with streaming stdio capture and timeout enforcement. |
| **OpenAI / Codex Runner**| (New requirement) | **ADAPTER** | `src/runtime/adapters/openai.py` | Provider-neutral adapter for OpenAI Codex app-server / agent execution. |
| **SQLite Storage** | `templates/migrations/sqlite/` | **ADAPTER** | `src/storage/sqlite/` | Production-grade SQLite adapter using WAL mode, busy timeouts, and atomic sequence increments. |
| **Postgres Storage** | `templates/migrations/postgres/` | **ADAPTER** | `src/storage/postgres/` | Full ACID Postgres adapter with connection pooling and `SKIP LOCKED` row claiming. |
| **Model Context Protocol**| (New requirement) | **ADAPTER** | `src/tools/mcp/` | Standard MCP JSON-RPC host implementation for agent tool invocation. |
| **A2A Delegation** | (New requirement) | **ADAPTER** | `src/protocols/a2a/` | A2A Task server endpoint and client adapter for remote agent delegation. |
| **OpenTelemetry Tracing** | (Ad-hoc logging) | **CORE INTEGRATION**| `src/observability/tracing/` | Full OpenTelemetry tracing adopting GenAI semantic conventions (`gen_ai.*`). |
| **Anti-Stall Prompt** | `templates/prompts/anti_stall_prompt_template.md` | **PLUGIN / TEMPLATE**| `src/templates/prompts/` | Cleaned of project-specific references; packaged as standard harness guidance for long-running agents. |
| **Auto Reconcile Script** | `templates/scripts/auto_reconcile.py` | **REMOVE** | Replaced by single DB truth | Abolished dual source of truth (Markdown vs DB); heuristic file reconciliation is no longer necessary. |
| **Clinical / Astra Leaks** | `lease.py`, `state.py`, `cli.py` | **REMOVE** | Completely purged | All healthcare domain terms (`clinical`, `ASTRA_DB_*`, etc.) eliminated to pass smoke tests. |
| **Template Script Copying**| `src/orchestrator_runtime/cli.py` | **REMOVE / REPLACE** | `src/cli/` & standard package | Replaced brittle file-copying CLI with standard Python package, CLI commands (`orchestrator run`, `worker`, etc.), and importable SDK. |
