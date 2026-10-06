# Evaluation, Chaos Testing & Conformance Specification

## 1. Scope & Objective

This specification details the formal testing methodology required to validate any implementation of the orchestrator specification.

---

## 2. Mandatory Test Categories

### 2.1 State-Machine Invariant Property Tests
- **Framework:** `hypothesis` stateful testing (`RuleBasedStateMachine`).
- **Objective:** Generate thousands of random interleaved operations (`create`, `claim`, `heartbeat`, `expire`, `verify`, `approve`, `complete`, `cancel`) and assert that invariants INV-001 through INV-012 hold after every step.

### 2.2 Concurrency & Race Condition Tests
- **Scenarios Tested:**
  1. **Simultaneous Claims:** 20 concurrent threads attempt to claim the same eligible work simultaneously. Exactly one must succeed; 19 must fail or claim subsequent work without deadlocking.
  2. **Stale Completion:** Worker A claims lease, pauses. Worker B claims lease with incremented token. Worker A attempts completion. Worker A must receive HTTP 409 Conflict.
  3. **Simultaneous Approval & Cancellation:** A human approves while another supervisor cancels. The state machine must resolve atomically without invalid intermediate states.

### 2.3 Fault Injection & Crash Recovery Tests
- **Injectable FailPoints:** Storage implementations must support artificial failpoints:
  - Fail immediately before writing outbox event.
  - Fail immediately after granting lease.
  - Simulate worker SIGKILL during tool execution.
- **Recovery Assertion:** On restart, the reconciliation loop must restore the system to a clean, consistent state within one sweep.

### 2.4 Language-Neutral Conformance Suite
- Stored as declarative YAML files in `conformance/scenarios/*.yaml`.
- Executed by a standard conformance test runner.
- Every release must achieve a 100% pass rate across all conformance scenarios.
