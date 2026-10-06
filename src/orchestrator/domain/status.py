"""Domain status enumerations and actor roles for the orchestrator."""

from enum import Enum


class WorkStatus(str, Enum):
    """Lifecycle status of a Work unit."""
    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    RUNNING = "RUNNING"
    WAITING_VERIFICATION = "WAITING_VERIFICATION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (WorkStatus.SUCCEEDED, WorkStatus.FAILED, WorkStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (WorkStatus.RUNNING, WorkStatus.WAITING_VERIFICATION, WorkStatus.AWAITING_APPROVAL)


class ExecutionStatus(str, Enum):
    """Lifecycle status of a single Execution attempt."""
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CANCELLED,
        )


class ActorRole(str, Enum):
    """Authorization role of the actor attempting a transition or tool call."""
    ANONYMOUS = "ANONYMOUS"
    AGENT_WORKER = "AGENT_WORKER"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"
    AUTHORIZED_VERIFIER = "AUTHORIZED_VERIFIER"
    SYSTEM_RECONCILER = "SYSTEM_RECONCILER"
    SCHEDULER = "SCHEDULER"


class VerifierVerdict(str, Enum):
    """Outcome verdict of an independent verification check."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
