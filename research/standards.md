# Protocols & Standards for Durable Agent Orchestration

## 1. Executive Summary

A core objective of this re-architecture is to build an **open standard and reference implementation** rather than a proprietary silo (Principle 6 & 7). We investigate established and emerging specifications from the Linux Foundation (AAIF, CNCF), IETF, W3C, and industry standards bodies.

We establish the standard boundaries:
- **MCP (Model Context Protocol):** The standard for **Agent-to-Tool** and **Agent-to-Context** interaction.
- **A2A (Agent-to-Agent):** The standard for **Orchestrator-to-Remote-Agent** communication.
- **OpenTelemetry + W3C Trace Context:** The standard for **Distributed Observability & Tracing**.
- **CloudEvents 1.0:** The standard for **Durable Orchestration Event Envelopes**.
- **IETF Standards (RFC 9110, RFC 9457, Idempotency-Key):** The standard for **HTTP APIs & Error Handling**.

---

## 2. Standards Adoption Matrix

| Standard / Specification | Governing Body | Architectural Role | Strategy | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **Model Context Protocol (MCP)** | Agentic AI Foundation / Anthropic | Agent $\leftrightarrow$ Tool / Resource boundary | **ADOPT** | Universal industry standard for connecting models to local and remote tools. Never reinvent tool calling. |
| **Agent-to-Agent (A2A)** | Linux Foundation | Orchestrator $\leftrightarrow$ External Autonomous Agent boundary | **ADOPT (Adapter)** | Provides standard Agent Cards and Task message protocols for cross-organization agent delegation. |
| **OpenTelemetry (OTel) + GenAI SemConv** | CNCF | Traces, metrics, spans, token telemetry | **ADOPT** | Universal telemetry standard. Adopting `gen_ai.*` semantic conventions allows instant visualization in Jaeger, Datadog, Honeycomb. |
| **CloudEvents 1.0** | CNCF | Orchestrator event bus envelopes | **ADOPT** | Provides unambiguous envelope structure (`id`, `source`, `type`, `time`, `datacontenttype`, `data`) preventing proprietary JSON silos. |
| **W3C Trace Context (RFC / REC)** | W3C | Distributed trace propagation (`traceparent`, `tracestate`) | **ADOPT** | Propagates trace context across orchestrator, worker sandboxes, and MCP tool servers. |
| **IETF Idempotency-Key Header** | IETF HTTPAPI WG | Safe retry of HTTP API requests | **ADOPT** | Standardized mechanism for deduplicating API calls and task claims across network failures. |
| **RFC 9457 Problem Details** | IETF | HTTP error responses (`type`, `title`, `status`, `detail`, `instance`) | **ADOPT** | Replaces ad-hoc error JSON with standard machine-readable error formats. |
| **OAuth 2.1 & RFC 8707 Resource Indicators** | IETF | Scoped authorization for tools & agent endpoints | **ADOPT** | Restricts tool tokens to specific audience endpoints, mitigating confused deputy attacks. |
| **SPIFFE / SPIRE** | CNCF | Cryptographic workload identity for agent sandboxes | **EXTEND / COMPATIBLE** | Provides x509 SVIDs for zero-trust worker authentication in enterprise deployments. |
| **in-toto / SLSA / Sigstore** | OpenSSF / Linux Foundation | Cryptographic provenance for generated artifacts | **ADOPT (Artifacts)** | Generates tamper-evident cryptographic provenance attestations for code and files created by agents. |
| **AGENTS.md** | Linux Foundation (AAIF) | Project-level instructions for coding agents | **ADOPT** | Standardized documentation file read by agents entering a repository. |
| **Serverless Workflow (CNCF)** | CNCF | Declarative workflow definition | **INTERNAL (Borrow concepts)** | Overly verbose DSL for general cloud functions; we borrow its state taxonomy while keeping Python/YAML agent definitions simple. |

---

## 3. Deep Dive: Model Context Protocol (MCP)

### 3.1 What MCP Solves
MCP standardizes:
1. **Tools:** JSON-RPC discovery and invocation of executable functions with JSON Schema input definitions.
2. **Resources:** Read-only data endpoints (files, database tables, documentation) with URI schemes.
3. **Prompts:** Pre-defined conversational templates exposed by servers.
4. **Sampling:** Server-initiated requests asking the client to run an LLM completion.
5. **Transports:** `stdio` (local process pipes) and `Streamable HTTP / SSE` (remote servers).

### 3.2 Where MCP Fits in the Orchestrator
MCP is strictly an **Agent $\leftrightarrow$ Tool interface**.
- The Orchestrator does **NOT** replace MCP.
- The Orchestrator acts as an **MCP Host / Gateway**:
  - Spawns authorized MCP servers inside the agent sandbox.
  - Intercepts tool calls to enforce security policies and rate limits.
  - Emits OpenTelemetry spans for every tool invocation.

---

## 4. Deep Dive: Agent-to-Agent (A2A) Protocol

### 4.1 What A2A Solves
A2A (originated by Google/Linux Foundation) defines a standard protocol for discovering, negotiating, and delegating work between independent AI agents:
- **Agent Card:** Machine-readable metadata describing an agent's capabilities, authentication requirements, and skills.
- **Task Lifecycle:** Standardized states (`SUBMITTED`, `WORKING`, `INPUT_REQUIRED`, `COMPLETED`, `FAILED`, `CANCELLED`).
- **Artifacts:** Structured outputs produced by the remote agent.

### 4.2 Where A2A Fits in the Orchestrator
The Orchestrator provides an **A2A Adapter**:
- External systems can submit work to the Orchestrator via an A2A server endpoint.
- The Orchestrator can delegate long-running sub-tasks to remote external agents via an A2A client adapter.

---

## 5. Event Envelope Specification: CloudEvents 1.0

To prevent proprietary event silos, all domain events emitted by the orchestrator adhere to CloudEvents 1.0 JSON format:

```json
{
  "specversion": "1.0",
  "id": "evt_01J9X8R5M7Q2P3K4N5B6V7C8",
  "source": "/orchestrator/work/TKT-1042/exec/ex_01J9X8",
  "type": "io.orchestrator.execution.started",
  "time": "2026-10-06T15:00:00.123Z",
  "datacontenttype": "application/json",
  "correlation_id": "corr_01J9X8R5M7",
  "causation_id": "evt_01J9X8R5M0",
  "data": {
    "work_id": "TKT-1042",
    "execution_id": "ex_01J9X8",
    "attempt": 1,
    "fencing_token": 42,
    "agent_id": "agent_coder_v2",
    "runtime": "subprocess"
  }
}
```

---

## 6. What Must Never Be Reinvented

1. **DO NOT reinvent tool protocols:** Never create a proprietary tool execution protocol when MCP exists.
2. **DO NOT invent custom telemetry formats:** Never create proprietary logging or metrics formats when OpenTelemetry is standard.
3. **DO NOT invent custom event envelope schemas:** Use CloudEvents 1.0.
4. **DO NOT invent custom HTTP error structures:** Use RFC 9457 (`application/problem+json`).
5. **DO NOT invent custom idempotency semantics:** Use the standard `Idempotency-Key` HTTP header.

---

## 7. Implications for the Orchestrator Architecture

1. **MUST:** Implement an MCP tool gateway that exposes tools to worker agents via standard JSON-RPC over `stdio` or HTTP/SSE.
2. **MUST:** Format all domain events to conform to the CNCF CloudEvents 1.0 specification.
3. **MUST:** Instrument all state transitions, scheduling loops, and tool invocations with OpenTelemetry spans matching GenAI semantic conventions.
4. **MUST:** Expose an A2A-compliant Task API for inter-agent delegation.
5. **MUST:** Enforce RFC 9457 error responses across all REST and control plane endpoints.
