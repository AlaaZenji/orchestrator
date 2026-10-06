# Orchestrator Platform: Reference Implementation for Durable AI Agent Orchestration

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-30%2F30%20passing-success.svg)](tests/)
[![Specification](https://img.shields.io/badge/spec-v0.1-orange.svg)](spec/)
[![Standards](https://img.shields.io/badge/standards-CloudEvents%20%7C%20OTel%20%7C%20MCP%20%7C%20A2A-purple.svg)](spec/PROTOCOL_BOUNDARIES.md)

**A production-grade, implementation-neutral, standards-compliant platform for durable multi-agent orchestration.**

---

## 1. What is this?

The **Orchestrator Platform** is an open standard and reference architecture that bridges the gap between **probabilistic AI intelligence** and **deterministic distributed systems guarantees**.

Most multi-agent frameworks delegate fundamental consistency, locking, and recovery to LLMs or fragile in-memory python loops. When workers crash, network sockets stall, or process pauses occur, state is lost, work is duplicated, and repositories are corrupted by stale writers.

The Orchestrator Platform solves this from first principles:
- **Deterministic Infrastructure:** State transitions, leases, fencing tokens, retries, dependency DAGs, sandboxes, and verification gates are handled by deterministic software.
- **Probabilistic Intelligence:** Planning, reasoning, code generation, and review are handled by LLM agents.
- **Provider-Neutral Kernel:** Zero vendor lock-in. Claude, OpenAI Codex, Gemini, or local models plug into an abstract `AgentRuntime` interface.
- **Industry Standards Compliance:** Native **CloudEvents 1.0**, **OpenTelemetry**, and **Model Context Protocol (MCP)**.
- **Zero-Friction Deployment:** Embedded **SQLite (WAL mode)** for local developer workflows; scales seamlessly to **PostgreSQL (ACID, `SKIP LOCKED`)** for production fleets.

---

## 2. Architecture Overview

```text
+-----------------------------------------------------------------------------------+
|                        ORCHESTRATOR ARCHITECTURE LAYERS                           |
+-----------------------------------------------------------------------------------+
| CONTROL PLANE (KERNEL)                                                            |
|  - Finite State Machine (spec/STATE_MACHINE.md)                                   |
|  - Monotonic Fencing Token Generator (Kleppmann-correct)                          |
|  - Priority Fair-Share Concurrency Scheduler                                      |
|  - Transactional Outbox (CloudEvents 1.0)                                         |
|  - Distributed Tracing (OpenTelemetry GenAI SemConv)                              |
+-----------------------------------------------------------------------------------+
       |                                     |                               |
       v                                     v                               v
[ STORAGE ADAPTERS ]               [ RUNTIME ADAPTERS ]            [ PROTOCOL ADAPTERS ]
- SQLite (WAL mode, embedded)      - Claude Code (CLI / SDK)       - MCP (JSON-RPC 2.0)
- PostgreSQL (ACID, SKIP LOCKED)   - OpenAI Codex (app-server)     - A2A (Task Protocol)
- In-Memory (fuzzing & testing)    - Subprocess / Local Shell      - Outbox Webhooks
                                   - Mock / Chaos Simulator
```

---

## 3. Five-Minute Quickstart

### Installation
```bash
pip install -e .
```

### CLI Usage (Zero Configuration)
The CLI operates immediately using an embedded SQLite database in `.orchestrator/state.db`:

```bash
# 1. Create a Work unit
orchestrator create TASK-101 "Implement JWT authentication" --priority 5

# 2. Inspect status
orchestrator status TASK-101

# 3. List active works
orchestrator list

# 4. Run crash reconciliation sweep
orchestrator reconcile
```

### Python SDK Usage
```python
import asyncio
from orchestrator import OrchestratorService, SQLiteStorageBackend, WorkStatus

async def main():
    storage = SQLiteStorageBackend("state.db")
    service = OrchestratorService(storage=storage)

    # Create work
    work = await service.create_work(
        work_id="TKT-01",
        title="Refactor database schema",
        priority=10
    )

    # Claim lease with atomic monotonic fencing token
    claimed_work, lease = await service.claim_work("TKT-01", worker_id="worker-agent-1")
    print(f"Claimed with fencing token: {lease.fencing_token}")

    # Renew lease via heartbeat
    await service.heartbeat("TKT-01", "worker-agent-1", lease.fencing_token)

asyncio.run(main())
```

---

## 4. Key Guarantees & Formal Invariants

| Invariant | Guarantee | Enforcement Mechanism |
| :--- | :--- | :--- |
| **INV-001** | Mutual exclusion: at most one active execution per work | Relational lease table with unique work constraint |
| **INV-002** | Stale writers strictly rejected | Kleppmann fencing check: `WHERE fencing_token = :token` |
| **INV-003** | Total event auditability | Transactional outbox committing CloudEvents 1.0 atomically |
| **INV-004** | Agent cannot self-complete work | State machine authorization guard rejects agent transitions to `SUCCEEDED` |
| **INV-007** | Crash reconstruction | Automated reconciliation sweep queries expired leases on DB clock |
| **INV-009** | Quarantined side effects | Ephemeral sandbox workspaces; fenced promotion gate |

---

## 5. Performance Benchmarks

Measured on standard commodity developer hardware:

| Benchmark Scenario | Throughput | Mean Latency |
| :--- | :--- | :--- |
| **In-Memory Work Creation** | **139,063 ops/sec** | **0.007 ms/op** |
| **In-Memory Lease Claims** | **77,522 ops/sec** | **0.013 ms/op** |
| **In-Memory Verification Submissions** | **145,281 ops/sec** | **0.007 ms/op** |
| **SQLite (WAL Mode) Persistent Work Creation**| **1,375 ops/sec** | **0.727 ms/op** |
| **SQLite (WAL Mode) Persistent Lease Claims** | **658 ops/sec** | **1.519 ms/op** |

To run benchmarks yourself:
```bash
python3 benchmarks/run_benchmarks.py
```

---

## 6. Testing & Conformance

The test suite validates correctness across multiple dimensions:
- **Unit Tests:** State transitions, authorization guards, and protocol parsers (`tests/unit/`).
- **Property Tests:** Stateful invariant fuzzing using `hypothesis` (`tests/property/`).
- **Concurrency Tests:** 50-worker claim races and stale worker rejection (`tests/concurrency/`).
- **Fault Injection:** Injectable crash points and recovery sweeps (`tests/fault_injection/`).
- **Integration Tests:** End-to-end SQLite DAG pipelines (`tests/integration/`).
- **Conformance Suite:** Language-neutral declarative YAML test scenarios (`conformance/scenarios/`).

Run the full test suite:
```bash
python3 -m pytest tests/ -v
```

---

## 7. Documentation Index

- **[Formal Specifications (`spec/`)](spec/)**:
  - [Problem Formalization (`spec/PROBLEM.md`)](spec/PROBLEM.md)
  - [Domain Model (`spec/DOMAIN_MODEL.md`)](spec/DOMAIN_MODEL.md)
  - [State Machine (`spec/STATE_MACHINE.md`)](spec/STATE_MACHINE.md)
  - [Formal Invariants (`spec/INVARIANTS.md`)](spec/INVARIANTS.md)
  - [Leases & Fencing Tokens (`spec/LEASES_AND_FENCING.md`)](spec/LEASES_AND_FENCING.md)
  - [Durable Execution (`spec/DURABLE_EXECUTION.md`)](spec/DURABLE_EXECUTION.md)
  - [Idempotency (`spec/IDEMPOTENCY.md`)](spec/IDEMPOTENCY.md)
  - [CloudEvents Model (`spec/EVENT_MODEL.md`)](spec/EVENT_MODEL.md)
  - [Protocol Boundaries (`spec/PROTOCOL_BOUNDARIES.md`)](spec/PROTOCOL_BOUNDARIES.md)
  - [Security Specification (`spec/SECURITY.md`)](spec/SECURITY.md)
  - [Evaluation & Conformance (`spec/EVALUATION.md`)](spec/EVALUATION.md)
- **[Research Foundation (`research/`)](research/)**:
  - [Distributed Systems Foundations (`research/distributed-systems.md`)](research/distributed-systems.md)
  - [Agent Architectures & Runtimes (`research/agent-architectures.md`)](research/agent-architectures.md)
  - [Durable Execution & Workflow Engines (`research/durable-execution.md`)](research/durable-execution.md)
  - [Protocols & Standards (`research/standards.md`)](research/standards.md)
  - [Production Systems Forensics (`research/production-systems.md`)](research/production-systems.md)
  - [Adversarial Red Team Reports (`research/red-team-*.md`)](research/)
  - [Competitive Analysis (`research/competitive-analysis.md`)](research/competitive-analysis.md)
  - [Forensic System Audit (`research/current-system-audit.md`)](research/current-system-audit.md)
- **[Architectural Decision Record (`ARCHITECTURE_DECISION.md`)](ARCHITECTURE_DECISION.md)**
- **[Capability Migration Matrix (`docs/MIGRATION_MATRIX.md`)](docs/MIGRATION_MATRIX.md)**
- **[Final Architecture Report (`FINAL_ARCHITECTURE_REPORT.md`)](FINAL_ARCHITECTURE_REPORT.md)**

---

## 8. License

MIT License. See [LICENSE](LICENSE) for details.
