# Problem Formalization: What is Durable Agent Orchestration?

## 1. Scope & Objective

This document formalizes the fundamental problem of orchestrating autonomous AI agents in durable, multi-step, distributed software engineering environments.

We provide formal, implementation-independent definitions of the core concepts, removing ambiguity before software design begins.

---

## 2. Formal Definitions

### 2.1 What is a Work?
A **Work** (historically termed "Task" or "Ticket") is a persistent, stateful unit of intended progress in a problem domain.
- A Work defines an objective, inputs, constraints, dependencies, priority, and completion criteria.
- A Work has a unique, immutable identity: $\text{WorkId} \in \mathbb{U}$.
- A Work survives system crashes, orchestrator restarts, worker failures, and network partitions.
- A Work may require multiple execution attempts before reaching a terminal status.

### 2.2 What is an Execution?
An **Execution** is a single, bounded, observable attempt to fulfill a Work by an Agent.
- An Execution has an identity: $\text{ExecutionId} \in \mathbb{U}$, bound to exactly one $\text{WorkId}$.
- An Execution is associated with an attempt number $k \in \{1, 2, \dots, N\}$.
- An Execution is governed by an active lease and a monotonic fencing token.
- If an Execution fails, stalls, or is cancelled, it enters a terminal failure state; retries produce a *new, distinct Execution*.

### 2.3 What is an Agent?
An **Agent** is an entity capable of probabilistic reasoning, tool invocation, and decision-making driven by one or more Large Language Models (LLMs) or autonomous policies.
- An Agent is treated as an untrusted actor with respect to infrastructure guarantees.
- An Agent produces candidate outputs, tool calls, and proposed state changes.
- An Agent cannot directly declare a Work complete or mutate authoritative state without validation.

### 2.4 What is a Runtime?
A **Runtime** is the operational environment and harness that hosts and executes an Agent process.
- Examples include a local subprocess, Claude Code harness, OpenAI Codex container, or remote A2A worker.
- The Runtime exposes a standard lifecycle interface (`start`, `send_message`, `pause`, `resume`, `cancel`, `wait`).

### 2.5 What is a Resource?
A **Resource** is an external or shared entity that an Agent interacts with or mutates during an Execution.
- Examples: A Git repository, filesystem directory, database instance, cloud sandbox, or external REST API.
- Resources may be *token-aware* (validating fencing tokens directly) or *token-unaware* (requiring fenced proxying or isolated branch staging).

### 2.6 What is an Artifact?
An **Artifact** is an immutable, content-addressed output produced during an Execution.
- Examples: A Git commit patch, a compiled binary, a test report, a log file, or an architectural markdown document.
- An Artifact has a cryptographic digest (SHA-256) and cannot be mutated once written.
- Artifacts produced during an Execution are *candidate artifacts* until promoted by a verification gate.

### 2.7 What is an Event?
An **Event** is an immutable record that a specific domain occurrence took place at a defined logical instant.
- Every state transition, lease grant, tool execution, and verification produces an Event.
- Events follow the CNCF CloudEvents 1.0 specification.
- Events provide total causal ordering per Work via monotonic sequence numbers.

### 2.8 What is a Workflow?
A **Workflow** is a directed graph (typically a DAG) of Work units connected by dependency and dataflow edges:
$$G = (V, E), \quad V = \{\text{Work}_1, \dots, \text{Work}_n\}, \quad E \subseteq V \times V$$
A Work $\text{Work}_B$ becomes eligible for execution if and only if all dependencies in $E$ have reached `SUCCEEDED` status.

### 2.9 What is a Policy?
A **Policy** is a deterministic, rule-based predicate evaluated by the orchestrator to govern execution permissions, budgets, transitions, or approvals:
$$\text{Policy}(\text{Context}, \text{Action}) \rightarrow \{\text{ALLOW}, \text{DENY}, \text{REQUIRE\_HUMAN\_APPROVAL}\}$$
Policies are evaluated outside the LLM.

### 2.10 What is a Verification?
A **Verification** is an independent, objective evaluation of candidate artifacts against declared criteria.
- Verification is performed by deterministic test runners, linters, security scanners, or human reviewers.
- Verification is decoupled from the worker agent that produced the code.

### 2.11 What is an Approval?
An **Approval** is an explicit authorization granted by an external authority (typically a human operator or authorized governance system) allowing a Work to proceed past a designated safety gate.

### 2.12 What is a Lease?
A **Lease** is a time-bounded grant of exclusive execution rights over a Work issued by the central authority to a specific worker.
- Leases are evaluated strictly against the authoritative store's monotonic clock.

### 2.13 What is a Fencing Token?
A **Fencing Token** is a strictly monotonically increasing integer sequence number issued on every lease grant that enables resources and storage to reject operations from stale workers.
