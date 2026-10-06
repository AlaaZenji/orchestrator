# Protocol Boundaries & Standards Integration Specification

## 1. Scope & Layering Architecture

To avoid building an isolated proprietary silo, the orchestrator adopts established open industry standards across all network and process boundaries:

```
+---------------------------------------------------------------------------------+
|                       DISTRIBUTED PROTOCOL BOUNDARIES                           |
+---------------------------------------------------------------------------------+
|                                                                                 |
|  [ External Orchestrators / Multi-Org Systems ]                                 |
|                         |                                                       |
|                         | Protocol: A2A (Agent-to-Agent REST + SSE)             |
|                         v                                                       |
|  +---------------------------------------------------------------------------+  |
|  |                    ORCHESTRATOR CONTROL PLANE (KERNEL)                    |  |
|  |                                                                           |  |
|  |  - State Machine & Invariants               - CloudEvents 1.0 Outbox      |  |
|  |  - Fencing Token Generator                  - OpenTelemetry Tracing       |  |
|  |  - Fair-Share Concurrency Scheduler        - ACID Storage (Postgres/SQLite)| |
|  +---------------------------------------------------------------------------+  |
|                         |                                                       |
|                         | Interface: AgentRuntime API                           |
|                         v                                                       |
|  +---------------------------------------------------------------------------+  |
|  |                       AGENT WORKER / RUNTIME SANDBOX                      |  |
|  |                                                                           |  |
|  |  - Local Process / Container Sandbox (Bubblewrap / Docker / gVisor)        |  |
|  |  - LLM Agent Harness (Claude Code / Codex / Custom Python / Subprocess)    |  |
|  |  - Ephemeral Workspace & Git Staging Branch                               |  |
|  +---------------------------------------------------------------------------+  |
|                         |                                                       |
|                         | Protocol: Model Context Protocol (MCP JSON-RPC)       |
|                         v                                                       |
|  +---------------------------------------------------------------------------+  |
|  |                         MCP TOOL / RESOURCE SERVERS                       |  |
|  |                                                                           |  |
|  |  - Filesystem Tools      - Git Gateway Tools     - Terminal / Exec Tools  |  |
|  |  - Database Tools        - Web Search Tools      - Policy Interceptor     |  |
|  +---------------------------------------------------------------------------+  |
|                                                                                 |
+---------------------------------------------------------------------------------+
```

---

## 2. Protocol Boundaries Breakdown

### 2.1 Agent-to-Tool Boundary: Model Context Protocol (MCP)
- **Role:** How the agent invokes functions and retrieves resources.
- **Specification:** Model Context Protocol (Agentic AI Foundation / Linux Foundation).
- **Rule:** The orchestrator never invents custom tool protocols. All agent tools are registered and invoked as MCP tools.
- **Transport:** Standard input/output (`stdio`) for local sandbox processes; Streamable HTTP/SSE for remote tool services.

### 2.2 Orchestrator-to-Remote-Agent Boundary: Agent-to-Agent (A2A)
- **Role:** How external autonomous systems delegate tasks to or receive tasks from the orchestrator.
- **Specification:** A2A Protocol (Linux Foundation).
- **Mapping:**
  - `A2A Task` $\leftrightarrow$ `Orchestrator Work`
  - `A2A Task State` $\leftrightarrow$ `Orchestrator WorkStatus`
  - `A2A Artifact` $\leftrightarrow$ `Orchestrator Artifact`

### 2.3 Observability Boundary: OpenTelemetry (OTel)
- **Role:** Distributed tracing, metrics, and log correlation.
- **Specification:** OpenTelemetry GenAI Semantic Conventions (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`).
- **Context Propagation:** W3C Trace Context headers (`traceparent`, `tracestate`) are injected into all worker environment variables and MCP requests.

### 2.4 Event Bus Boundary: CloudEvents 1.0
- **Role:** Asynchronous notification and event replay.
- **Specification:** CNCF CloudEvents 1.0.
- **Format:** JSON-formatted event envelopes emitted through the transactional outbox.
