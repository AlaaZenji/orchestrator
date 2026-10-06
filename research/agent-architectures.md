# Agent Architectures & Runtime Interfaces — Research Report

## 1. Executive Summary

This report surveys the state of the art in LLM agent architectures, harnesses, and runtime execution models. We analyze primary sources from Anthropic, OpenAI, Google, Microsoft, and open-source systems.

A key insight emerges from modern production agent systems: **the orchestrator must not attempt to be an in-process agent framework**. Attempting to model every LLM token, prompt template, or internal chain-of-thought inside the distributed orchestration engine leads to fatal coupling, fragile state persistence, and vendor lock-in.

Instead, the industry has converged on a clear architectural split:
- **Agent Harness (Execution Worker):** Manages the prompt loop, context compaction, tool selection, reasoning trajectory, and model provider API.
- **Orchestrator (Control Plane):** Manages work lifecycle, dependency resolution, leases, fencing, durability, resource isolation, verification gates, approvals, and distributed events.

---

## 2. Primary Source Survey

### 2.1 Anthropic Research & Engineering
- **"Building Effective Agents" (Dec 2024):** [VERIFIED-PRIMARY] Argues against overly complex autonomous agent graphs. Recommends starting with simple patterns: Augmented LLM $\rightarrow$ Prompt Chaining $\rightarrow$ Routing $\rightarrow$ Parallelization $\rightarrow$ Orchestrator-Workers $\rightarrow$ Evaluator-Optimizer. Recommends treating multi-agent workflows as specialized orchestrators with strict schemas.
- **"How We Built Our Multi-Agent Research System" (June 2025):** [VERIFIED-PRIMARY]
  - Orchestrator-worker topology: Lead model (Opus 4) orchestrates specialized subagents (Sonnet 4).
  - Yielded +90.2% performance on BrowseComp vs single-agent baseline, but consumed 15x token volume.
  - Crucial engineering finding: *"Agents are stateful and errors compound... we can't just restart from the beginning; we need to resume from where the agent was."* Checkpoints and structured error feedback to the agent are mandatory.
  - Subagent sprawl: Unconstrained agents spawned 50+ recursive workers, entering infinite exploration loops. Hard concurrency limits and budget guards were required.
- **"Effective Harnesses for Long-Running Agents" & Context Engineering (2025):** [VERIFIED-PRIMARY]
  - Long-running tasks require context compaction, periodic summarization, and scratchpad memory files.
  - When context limits are exceeded, agents fail unpredictably. Context must be curated externally or pruned systematically.

### 2.2 OpenAI Agents SDK & Codex Platform
- **OpenAI Codex CLI & Cloud Execution:** [VERIFIED-PRIMARY]
  - Codex uses a client-server architecture with an execution protocol (app-server / JSON-RPC / SSE).
  - Code execution occurs inside isolated, network-restricted containers.
  - Tasks are defined declaratively in `AGENTS.md` or instruction manifests.
  - Agents produce git pull requests or patches; changes are never committed directly to main branches without CI validation.
- **OpenAI Agents SDK (`openai-agents-python`):** [VERIFIED-PRIMARY]
  - Formalizes **Handoffs** (transferring control from Agent A to Agent B with explicit input context).
  - Introduces deterministic **Guardrails** (input/output validation filters executed before and after LLM invocations).
  - Implements **Tracing** compatible with OpenTelemetry.

### 2.3 OpenAI Symphony
- **Architecture Analysis:** [VERIFIED-PRIMARY]
  - Symphony acts as a durable issue-tracker-driven orchestrator.
  - Periodically polls or receives webhooks from an issue tracking system (GitHub/Linear).
  - Spawns an isolated Codex worker per issue in an ephemeral workspace.
  - Worker lifecycle: `DISCOVER` $\rightarrow$ `CLAIM` $\rightarrow$ `RUN` $\rightarrow$ `VALIDATE` $\rightarrow$ `SUBMIT_PR`.
  - Symphony does not micromanage the Codex reasoning loop; it supervises the execution boundary, tracks workspace leases, and checks exit codes and PR status.

### 2.4 LangGraph, AutoGen, and CrewAI
- **LangGraph:** [VERIFIED-PRIMARY] Uses cyclic state graphs with checkpointers (Postgres, SQLite, Memory). Features `interrupt()` for human-in-the-loop approvals. Limitation: tight coupling between Python code graph definitions and execution state; schema migrations of graph state are notoriously brittle.
- **AutoGen & CrewAI:** Heavy in-memory abstractions, chat-centric conversation turns. Lack formal durable execution guarantees, lease fencing, or crash recovery.

---

## 3. The Core Dilemma: Micro-Step vs. Macro-Step Orchestration

A fundamental architectural choice in agent systems is the granularity of control:

```
+---------------------------------------------------------------------------------+
|                       GRANULARITY OF ORCHESTRATION                              |
+---------------------------------------------------------------------------------+
| APPROACH 1: MICRO-STEP ORCHESTRATION                                            |
| Every LLM call, prompt formatting, and tool execution is a durable workflow     |
| step in the orchestrator database.                                              |
|                                                                                 |
| Advantages: Every single token/tool call is persisted. Replay is granular.      |
| Disadvantages: Enormous database write amplification (1000s of events per task); |
| massive latency; schema brittleness; impossible to replay LLM steps identically |
| without cached exact token matching.                                            |
+---------------------------------------------------------------------------------+
| APPROACH 2: MACRO-STEP ORCHESTRATION (THE CONSENSUS PRODUCTION PATTERN)         |
| The orchestrator manages Work, Executions, Leases, Sandboxes, and Artifacts.     |
| The Agent Runtime executes the internal reasoning loop in a worker process,     |
| streaming structured events and checkpoints to the orchestrator.                |
|                                                                                 |
| Advantages: Implementation-neutral; supports any LLM (Claude, GPT, Gemini, Llama);|
| high performance; low database overhead; clean security and sandbox boundaries.  |
| Disadvantages: If a worker crashes mid-step, the retry replays from the last     |
| durable checkpoint or restarts the macro step.                                   |
+---------------------------------------------------------------------------------+
```

*Finding:* Production leaders (OpenAI Symphony, GitHub Copilot Coding Agent, Cognition Devin, Anthropic Research) overwhelmingly choose **Macro-Step Orchestration** with streaming event telemetry and periodic snapshot checkpoints.

---

## 4. The Minimal, Provider-Neutral Agent Runtime Interface

To ensure the orchestrator is implementation-neutral (Principle 6), the core orchestration engine must never import `anthropic`, `openai`, `google-genai`, or `langchain`.

Instead, the orchestrator interacts with agents through a clean, abstract **`AgentRuntime`** interface:

```python
class ExecutionContext:
    work_id: str
    execution_id: str
    fencing_token: int
    attempt: int
    inputs: dict[str, Any]
    resources: list[Resource]
    workspace_path: Path
    env_vars: dict[str, str]
    checkpoint_state: Optional[dict[str, Any]]

class AgentRuntime(ABC):
    """Provider-neutral interface for executing agent workloads."""

    @abstractmethod
    async def start(
        self,
        context: ExecutionContext,
        event_sink: Callable[[DomainEvent], Awaitable[None]]
    ) -> ExecutionHandle:
        """Launches the agent process/task in its configured sandbox."""
        ...

    @abstractmethod
    async def send_message(
        self,
        handle: ExecutionHandle,
        message: AgentMessage
    ) -> None:
        """Injects a message (e.g. human feedback, steering) into running agent."""
        ...

    @abstractmethod
    async def pause(self, handle: ExecutionHandle) -> Checkpoint:
        """Requests graceful pause; returns captured execution checkpoint."""
        ...

    @abstractmethod
    async def resume(
        self,
        handle: ExecutionHandle,
        checkpoint: Checkpoint
    ) -> None:
        """Resumes a paused execution from a checkpoint."""
        ...

    @abstractmethod
    async def cancel(
        self,
        handle: ExecutionHandle,
        grace_period_seconds: float = 15.0
    ) -> None:
        """Requests graceful cancellation (SIGTERM), then forceful termination (SIGKILL)."""
        ...

    @abstractmethod
    async def wait(self, handle: ExecutionHandle) -> ExecutionOutcome:
        """Awaits execution completion; returns status, artifacts, and exit code."""
        ...
```

### Adapters
Under this abstraction, different backends plug in seamlessly:
- **`ClaudeCodeRuntimeAdapter`:** Invokes the Claude Code CLI or Agent SDK with appropriate flags (`--dangerously-skip-permissions` inside sandboxes, or socket permission hooks).
- **`OpenAICodexRuntimeAdapter`:** Connects via JSON-RPC/SSE to the Codex app-server.
- **`SubprocessScriptAdapter`:** Runs local Python/Node scripts with standard I/O event streaming.
- **`RemoteA2ARuntimeAdapter`:** Delegates execution to an external agent over the A2A HTTP/REST protocol.
- **`MockTestingRuntimeAdapter`:** Deterministic simulator for fault injection, chaos testing, and state machine verification.

---

## 5. What the Orchestrator Must Know vs. What It Must Ignore

| Domain Concept | Orchestrator MUST Know | Orchestrator MUST Ignore |
| :--- | :--- | :--- |
| **Prompts & System Instructions** | Path to prompt template file or versioned ID; hash of the prompt for reproducibility. | The semantic tokens, internal chain-of-thought, or formatting logic. |
| **Model Parameters** | Requested model ID and temperature string (as configuration metadata). | Tokenizer mechanics, embedding dimensions, attention heads. |
| **Tool Execution** | Tool call events for audit and billing; permission authorization requests. | Internal python implementation of tools within the agent harness. |
| **State & Lifecycle** | Whether execution is `RUNNING`, `WAITING`, `AWAITING_APPROVAL`, `SUCCEEDED`, `FAILED`. | Internal agent loop counter or memory scratchpad contents (stored as opaque blobs). |
| **Outputs** | Declared artifacts (file paths, git commits, PR URLs) and verification verdict. | Intermediary draft responses abandoned during reasoning. |

---

## 6. Marketing Claims vs. Engineering Realities

| Marketing Claim | Engineering Reality |
| :--- | :--- |
| *"Autonomous multi-agent swarms self-organize to solve any enterprise problem."* | Unconstrained multi-agent swarms suffer from exponential error accumulation, infinite loop traps, and enormous token costs. Deterministic state machines with bounded worker pools are required in production. |
| *"Agents can directly verify and approve their own work."* | LLM evaluators suffer from self-preference bias, sycophancy, and shared hallucinations. Independent verification gates (unit tests, static analysis, deterministic policy, and human approvals) are mandatory. |
| *"Context is infinite; just load the entire codebase into prompt."* | Large contexts suffer from "needle in a haystack" degradation, high latency, and high cost. Selective context retrieval, workspace scoping, and file-based inspection outperform monolithic context stuffing. |
| *"Agents run reliably without infrastructure oversight."* | In production, agents hang on socket I/O, exhaust disk space, hit rate limits, and crash unexpectedly. Strict watchdogs, heartbeats, leases, and fencing are essential. |

---

## 7. Implications for the Orchestrator Architecture

1. **MUST:** Enforce the separation between Orchestrator Control Plane and Agent Runtime Harness.
2. **MUST:** Treat agent state snapshots/checkpoints as opaque serialized payloads stored alongside execution records.
3. **MUST:** Provide a first-class `AWAITING_APPROVAL` state allowing an agent execution to yield cleanly while awaiting human verification or external review.
4. **MUST:** Implement streaming event callbacks (`DomainEvent`) from the runtime to the orchestrator to populate the OpenTelemetry trace and audit logs without requiring synchronous database writes on every token.
5. **SHOULD:** Support external steering messages via `send_message()` to allow humans or supervisor agents to guide execution without killing the process.
