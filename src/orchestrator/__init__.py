"""Durable AI Agent Orchestration Platform — Production Reference Implementation.

A standards-based, implementation-neutral platform for deterministic agent orchestration.
Combines Kleppmann-correct monotonic fencing, Temporal-style timeout taxonomies,
CloudEvents 1.0, OpenTelemetry, and Model Context Protocol (MCP).
"""

__version__ = "0.1.0"

from orchestrator.domain import (
    Work,
    Execution,
    Lease,
    Artifact,
    VerificationResult,
    ApprovalRequest,
    DomainEvent,
    ActorContext,
    WorkStatus,
    ExecutionStatus,
    ActorRole,
    VerifierVerdict,
    OrchestratorError,
    StaleFencingTokenError,
    InvalidTransitionError,
    UnauthorizedTransitionError,
    LeaseExpiredError,
    WorkNotFoundError,
    DependencyUnsatisfiedError,
    ConcurrencyConflictError,
    IdempotencyConflictError,
)
from orchestrator.orchestration import (
    WorkStateMachine,
    ExecutionStateMachine,
    DependencyResolver,
    Scheduler,
)
from orchestrator.reliability import (
    LeaseManager,
    FencedGateway,
    IdempotencyManager,
    RetryPolicy,
    LeaseWatchdog,
    CrashReconciler,
)
from orchestrator.storage import (
    StorageBackend,
    MemoryStorageBackend,
    SQLiteStorageBackend,
)
from orchestrator.runtime import (
    AgentRuntime,
    ExecutionContext,
    ExecutionHandle,
    ExecutionOutcome,
    Checkpoint,
    MockAgentRuntime,
    SubprocessAgentRuntime,
    ClaudeCodeAgentRuntime,
    OpenAICodexAgentRuntime,
)
from orchestrator.sandbox import (
    SandboxProvider,
    LocalSandboxProvider,
    CommandResult,
    PathTraversalError,
)
from orchestrator.tools import (
    ToolDefinition,
    ToolResult,
    MCPToolHost,
)
from orchestrator.protocols import (
    AgentCard,
    A2ATaskStatus,
    A2AAdapter,
)
from orchestrator.events import (
    CloudEventFormatter,
    OutboxRelayer,
)
from orchestrator.observability import TelemetryTracer
from orchestrator.security import (
    SecurityPolicyEngine,
    CredentialBroker,
)
from orchestrator.api import OrchestratorService

__all__ = [
    "__version__",
    # Domain
    "Work",
    "Execution",
    "Lease",
    "Artifact",
    "VerificationResult",
    "ApprovalRequest",
    "DomainEvent",
    "ActorContext",
    "WorkStatus",
    "ExecutionStatus",
    "ActorRole",
    "VerifierVerdict",
    "OrchestratorError",
    "StaleFencingTokenError",
    "InvalidTransitionError",
    "UnauthorizedTransitionError",
    "LeaseExpiredError",
    "WorkNotFoundError",
    "DependencyUnsatisfiedError",
    "ConcurrencyConflictError",
    "IdempotencyConflictError",
    # Orchestration
    "WorkStateMachine",
    "ExecutionStateMachine",
    "DependencyResolver",
    "Scheduler",
    # Reliability
    "LeaseManager",
    "FencedGateway",
    "IdempotencyManager",
    "RetryPolicy",
    "LeaseWatchdog",
    "CrashReconciler",
    # Storage
    "StorageBackend",
    "MemoryStorageBackend",
    "SQLiteStorageBackend",
    # Runtime
    "AgentRuntime",
    "ExecutionContext",
    "ExecutionHandle",
    "ExecutionOutcome",
    "Checkpoint",
    "MockAgentRuntime",
    "SubprocessAgentRuntime",
    "ClaudeCodeAgentRuntime",
    "OpenAICodexAgentRuntime",
    # Sandbox
    "SandboxProvider",
    "LocalSandboxProvider",
    "CommandResult",
    "PathTraversalError",
    # Tools & Protocols
    "ToolDefinition",
    "ToolResult",
    "MCPToolHost",
    "AgentCard",
    "A2ATaskStatus",
    "A2AAdapter",
    # Events & Observability
    "CloudEventFormatter",
    "OutboxRelayer",
    "TelemetryTracer",
    # Security
    "SecurityPolicyEngine",
    "CredentialBroker",
    # Service API
    "OrchestratorService",
]
