# Evaluation, Reliability & Conformance — Research Report

## 1. Executive Summary

This report establishes the testing, evaluation, and conformance architecture for the orchestrator. High test coverage (e.g. 90% line coverage) is notoriously inadequate for distributed systems: tests can cover every line of code while completely missing race conditions, network partition anomalies, and stale worker split-brain states.

To build an open standard and a production-grade reference implementation, we require a **multi-tiered reliability pyramid**:
1. **Deterministic Graders & Property-Based Tests:** Proving state machine invariants under arbitrary state sequences.
2. **Injectable Fault Injection & Crash Points:** Validating recovery when the process is killed at every atomic step.
3. **Concurrency & Race Condition Harnesses:** Simulating simultaneous claims and stale worker updates.
4. **Language-Neutral Conformance Suite:** Enabling any future implementation (in Rust, Go, TypeScript) to verify compatibility against declarative YAML test scenarios.

---

## 2. Evaluation Methodologies: Agent vs. Distributed Systems

```
+---------------------------------------------------------------------------------+
|                       TWO DISTINCT TESTING DOMAINS                              |
+---------------------------------------------------------------------------------+
| DOMAIN 1: DISTRIBUTED CONTROL PLANE RELIABILITY (DETERMINISTIC)                 |
| Focus: State transitions, leases, fencing, persistence, idempotency, retries.   |
| Method: Formal property tests, state-machine fuzzing, deterministic fault       |
| injection, crash-restart loops, linearizability checkers.                       |
| Goal: 100% mathematical consistency; zero split-brain; zero lost updates.       |
+---------------------------------------------------------------------------------+
| DOMAIN 2: AGENT INTELLIGENCE & TRAJECTORY EVALUATION (PROBABILISTIC)           |
| Focus: Model reasoning quality, tool usage efficiency, prompt adherence.        |
| Method: SWE-bench Verified benchmarks, pass@k metrics, deterministic unit test  |
| grading of agent output artifacts, LLM-as-judge with strict rubrics.            |
| Goal: High task completion rate; bounded token cost; zero hallucinations in PRs.|
+---------------------------------------------------------------------------------+
```

---

## 3. Distributed Systems Testing Architecture

### 3.1 Lessons from FoundationDB, TigerBeetle VOPR & Antithesis
- **TigerBeetle Viewstamped Replication Fuzzer (VOPR):** Runs millions of state machine transitions against a simulated network and storage layer. Injected faults include dropped packets, bit rot, disk stalls, and sudden power failure.
- **FoundationDB:** Simulates entire distributed clusters within a single deterministic thread where pseudo-random numbers control time, networking, and disk latency.
- **Key Takeaway for Orchestrator:** We must design storage and runtime interfaces with **injectable failure hooks (`FailPoint`)** so that unit and integration tests can simulate:
  - Database connection loss immediately after lease grant but before execution start.
  - Process termination immediately before writing to the outbox.
  - Worker wake-up after lease expiration with stale token presentation.

### 3.2 Property-Based Stateful Testing (Hypothesis RuleBasedStateMachine)
Using `hypothesis.stateful.RuleBasedStateMachine`, we construct a model-based fuzzer:
- **Operations:** `create_work()`, `claim_work()`, `renew_lease()`, `expire_lease()`, `complete_work()`, `fail_work()`, `retry_work()`, `cancel_work()`.
- **Invariants Checked on Every Step:**
  - $\forall w \in \text{Work}$: If status is `RUNNING`, exactly one active lease exists with `fencing_token` matching $w.\text{active\_token}$.
  - Fencing tokens for a given work are strictly monotonically increasing.
  - A work in terminal status (`SUCCEEDED`, `FAILED`, `CANCELLED`) can never transition to any other status.
  - Number of execution records equals $1 + \text{retry\_count}$.

---

## 4. Designing a Language-Neutral Conformance Suite

To establish an open standard, the orchestrator specification must be testable independently of Python. We adopt the pattern of **Wasm Spec Tests, OCI Runtime Tools, and CloudEvents SDK Conformance**.

### 4.1 Declarative Scenario Format (`conformance/scenarios/*.yaml`)
Tests are specified as declarative YAML scenarios executing commands and asserting expected state and events:

```yaml
scenario: "stale-worker-rejected-by-fencing-token"
description: "Worker A claims lease, pauses until expiry, Worker B claims, Worker A write rejected."
steps:
  - action: "create_work"
    params: { work_id: "W-100", title: "Test task" }
    expect: { status: "ELIGIBLE" }

  - action: "claim_work"
    params: { work_id: "W-100", worker_id: "worker-A", lease_seconds: 5 }
    expect:
      status: "RUNNING"
      fencing_token: 1
      active_worker: "worker-A"

  - action: "fast_forward_time"
    params: { seconds: 10 }

  - action: "reconcile_leases"
    expect:
      work_status: "ELIGIBLE"
      event_type: "io.orchestrator.lease.expired"

  - action: "claim_work"
    params: { work_id: "W-100", worker_id: "worker-B", lease_seconds: 5 }
    expect:
      status: "RUNNING"
      fencing_token: 2
      active_worker: "worker-B"

  - action: "complete_execution"
    params: { work_id: "W-100", worker_id: "worker-A", fencing_token: 1 }
    expect_error:
      code: "STALE_FENCING_TOKEN"
      status: 409

  - action: "complete_execution"
    params: { work_id: "W-100", worker_id: "worker-B", fencing_token: 2 }
    expect:
      status: "SUCCEEDED"
      event_type: "io.orchestrator.work.succeeded"
```

Any implementation (Python, Go, Rust) provides a thin test runner CLI that parses these scenarios, executes them against its engine, and validates results.

---

## 5. Recommended Test Structure for Reference Implementation

```text
tests/
  ├── unit/                     # Fast in-memory unit tests for domain entities & state machines
  ├── property/                 # Hypothesis property & state-machine invariant fuzzing
  ├── concurrency/              # Multiprocess & multithreaded simultaneous claims and races
  ├── fault_injection/          # Crash-recovery tests using injectable failpoints
  ├── integration/              # Real SQLite and Postgres integration tests
  ├── security/                 # Sandbox boundary, path traversal, and token forgery tests
  ├── conformance/              # Conformance test runner executing YAML test scenarios
  └── benchmarks/               # Throughput and latency measurements
```

---

## 6. Implications for the Orchestrator Architecture

1. **MUST:** Implement a declarative Conformance Suite in YAML to serve as the definitive specification verification tool.
2. **MUST:** Build stateful property tests using `hypothesis` to explore interleaved concurrent state transitions.
3. **MUST:** Include an injectable `FailPoint` mechanism in the storage and execution engines to test crash recovery.
4. **MUST NOT:** Rely on sleep-based timing in tests; use deterministic mock clocks and explicit step triggers.
