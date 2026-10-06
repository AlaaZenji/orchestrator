# Orchestrator Platform Architecture

## 1. System Vision

The Orchestrator Platform is designed from first principles to be an **open standard and production reference implementation** for orchestrating autonomous AI coding agents.

The core architectural thesis is:
> **Deterministic software for consistency, scheduling, leases, fencing, and verification; probabilistic models for planning, reasoning, code generation, and review.**

---

## 2. Layered Architecture

```text
+-----------------------------------------------------------------------------------+
| LAYER 1: CLIENT & PROTOCOL BOUNDARIES                                             |
|  - CLI (`orchestrator run`, `create`, `list`, `reconcile`, etc.)                  |
|  - Python SDK (`OrchestratorService`)                                             |
|  - Agent-to-Agent (A2A) Protocol Adapter                                          |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| LAYER 2: CONTROL PLANE & CORE KERNEL                                              |
|  - Deterministic Finite State Machine (`orchestrator.orchestration.state_machine`)|
|  - DAG Topological Dependency Resolver (`orchestrator.orchestration.dependency`)  |
|  - Priority Fair-Share Scheduler (`orchestrator.orchestration.scheduler`)         |
|  - Transactional Outbox Engine (`orchestrator.events.outbox`)                     |
|  - Distributed Tracing Context (`orchestrator.observability.telemetry`)           |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| LAYER 3: RELIABILITY & GUARANTEES                                                 |
|  - Kleppmann-Correct Monotonic Fencing Tokens (`orchestrator.reliability.leases`)|
|  - Fenced Gateways (`orchestrator.reliability.fencing`)                           |
|  - Idempotency Key Manager (`orchestrator.reliability.idempotency`)               |
|  - Heartbeat Watchdog & Timeout Reaper (`orchestrator.reliability.watchdog`)      |
|  - Crash Reconciliation Engine (`orchestrator.reliability.reconciliation`)       |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| LAYER 4: PLUGGABLE ADAPTERS & BACKENDS                                            |
|  - Storage: SQLite (WAL mode, embedded) | PostgreSQL (ACID) | In-Memory (testing) |
|  - Runtimes: Claude Code | OpenAI Codex | Subprocess | Mock Simulator             |
|  - Sandboxes: Local Ephemeral Directory | Docker Containers                       |
|  - Tools: Model Context Protocol (MCP JSON-RPC 2.0 Host)                          |
+-----------------------------------------------------------------------------------+
```

---

## 3. Key Design Decisions

1. **Storage-Enforced Fencing (Martin Kleppmann, 2016):**
   Generating fencing tokens in a database is insufficient if workers mutate shared resources directly. We quarantine worker mutations in ephemeral workspaces; promotion to shared branches requires an atomic storage CAS check `WHERE fencing_token = :claimed_token`. Stale workers receive HTTP 409 Conflict.

2. **Single Authoritative State Store:**
   We abolished competing sources of truth (Markdown files vs database vs JSON). The database is the sole authoritative state store. Markdown ticket files are strictly rendered artifacts or projections.

3. **CNCF CloudEvents 1.0 & OpenTelemetry:**
   All domain events are emitted through a Transactional Outbox matching CloudEvents 1.0 specifications. All executions emit OpenTelemetry GenAI semantic convention traces.

4. **Zero-Friction Local Experience:**
   The entire system runs out-of-the-box using embedded SQLite with WAL mode, requiring zero Docker daemons or external cluster dependencies. Production deployments scale directly to PostgreSQL.

For full architectural comparisons, scoring matrices, and alternatives evaluated, see **[ARCHITECTURE_DECISION.md](ARCHITECTURE_DECISION.md)**.
