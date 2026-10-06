"""Provider-neutral Agent Runtime interface and execution abstractions."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable
from orchestrator.domain.models import DomainEvent, Artifact
from orchestrator.domain.status import ExecutionStatus


@dataclass(frozen=True)
class ExecutionContext:
    """Context passed to the runtime for executing an agent task."""
    work_id: str
    execution_id: str
    fencing_token: int
    attempt: int
    workspace_path: Path
    inputs: Dict[str, Any] = field(default_factory=dict)
    env_vars: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 3600.0
    checkpoint_state: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ExecutionHandle:
    """Opaque handle representing an actively executing agent process."""
    execution_id: str
    runtime_type: str
    process_id: Optional[int] = None
    native_handle: Any = None


@dataclass(frozen=True)
class Checkpoint:
    """Serialized snapshot of agent execution state for pause/resume."""
    execution_id: str
    step_index: int
    state_payload: Dict[str, Any]
    artifacts: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionOutcome:
    """The result of an agent execution attempt."""
    execution_id: str
    status: ExecutionStatus
    exit_code: int = 0
    candidate_artifacts: List[Artifact] = field(default_factory=list)
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentRuntime(ABC):
    """Abstract provider-neutral interface for executing AI agents."""

    @abstractmethod
    async def start(
        self,
        context: ExecutionContext,
        event_sink: Optional[Callable[[DomainEvent], Awaitable[None]]] = None,
    ) -> ExecutionHandle:
        """Launches the agent in its execution environment."""
        ...

    @abstractmethod
    async def send_message(self, handle: ExecutionHandle, message: str) -> None:
        """Injects steering input or human feedback into the running agent."""
        ...

    @abstractmethod
    async def pause(self, handle: ExecutionHandle) -> Checkpoint:
        """Requests graceful pause; captures and returns a checkpoint snapshot."""
        ...

    @abstractmethod
    async def resume(self, handle: ExecutionHandle, checkpoint: Checkpoint) -> None:
        """Resumes execution from a previously captured checkpoint."""
        ...

    @abstractmethod
    async def cancel(self, handle: ExecutionHandle, grace_period_seconds: float = 15.0) -> None:
        """Terminates the execution gracefully (SIGTERM), then forcefully (SIGKILL)."""
        ...

    @abstractmethod
    async def wait(self, handle: ExecutionHandle) -> ExecutionOutcome:
        """Awaits execution completion and returns the final outcome."""
        ...
