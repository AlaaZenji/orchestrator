"""Domain models, status enums, and exceptions."""

from orchestrator.domain.status import (
    WorkStatus,
    ExecutionStatus,
    ActorRole,
    VerifierVerdict,
)
from orchestrator.domain.models import (
    Work,
    Execution,
    Lease,
    Artifact,
    VerificationResult,
    ApprovalRequest,
    DomainEvent,
    ActorContext,
    utc_now,
)
from orchestrator.domain.exceptions import (
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

__all__ = [
    "WorkStatus",
    "ExecutionStatus",
    "ActorRole",
    "VerifierVerdict",
    "Work",
    "Execution",
    "Lease",
    "Artifact",
    "VerificationResult",
    "ApprovalRequest",
    "DomainEvent",
    "ActorContext",
    "utc_now",
    "OrchestratorError",
    "StaleFencingTokenError",
    "InvalidTransitionError",
    "UnauthorizedTransitionError",
    "LeaseExpiredError",
    "WorkNotFoundError",
    "DependencyUnsatisfiedError",
    "ConcurrencyConflictError",
    "IdempotencyConflictError",
]
