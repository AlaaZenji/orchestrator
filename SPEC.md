# Orchestrator Platform Specification (v0.1)

This document is the root entry point for the **Orchestrator Platform Formal Specification (Version 0.1)**.

The specification defines the implementation-neutral domain model, state machines, correctness invariants, fencing semantics, event schemas, and protocol boundaries required for durable AI agent orchestration.

---

## Specification Documents

1. **[Problem Formalization](spec/PROBLEM.md)**
   Formal definitions of Work, Execution, Agent, Runtime, Resource, Artifact, Event, Workflow, Policy, Verification, Approval, Lease, and Fencing Token.

2. **[Domain Model](spec/DOMAIN_MODEL.md)**
   Entity relationships, schemas, and immutability invariants for all core entities.

3. **[State Machine](spec/STATE_MACHINE.md)**
   Legal transitions, forbidden transitions, trigger actors, pre-conditions, and concurrency guards.

4. **[Formal Invariants](spec/INVARIANTS.md)**
   Non-negotiable invariants (INV-001 through INV-012) governing mutual exclusion, fencing, auditability, and recovery.

5. **[Leases & Fencing](spec/LEASES_AND_FENCING.md)**
   Mathematical proofs, sequence allocation, storage-side verification, and the Fenced Gateway pattern.

6. **[Durable Execution](spec/DURABLE_EXECUTION.md)**
   Deterministic behavior under crashes, hangs, network partitions, API exhaustion, and worker returns.

7. **[Idempotency](spec/IDEMPOTENCY.md)**
   Idempotency key generation, caching boundaries, and duplicate delivery resolution across all operations.

8. **[Event Model](spec/EVENT_MODEL.md)**
   CNCF CloudEvents 1.0 JSON format, sequence numbering, ordering, and transactional outbox.

9. **[Protocol Boundaries](spec/PROTOCOL_BOUNDARIES.md)**
   Integration boundaries for Model Context Protocol (MCP), Agent-to-Agent (A2A), and OpenTelemetry.

10. **[Security Specification](spec/SECURITY.md)**
    Zero-trust agent sandbox boundaries, path traversal defenses, credential isolation, and actor authorization.

11. **[Evaluation & Conformance](spec/EVALUATION.md)**
    Hypothesis property testing, fault injection failpoints, and declarative YAML conformance suites.

---

## Versioning & Governance
- **Current Version:** `0.1` (see [spec/SPEC_VERSION](spec/SPEC_VERSION))
- **Status:** Frozen Reference Specification
