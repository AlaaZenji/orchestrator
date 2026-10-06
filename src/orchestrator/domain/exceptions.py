"""Domain exceptions for orchestrator invariants, fencing, and state machines."""


class OrchestratorError(Exception):
    """Base exception for all orchestrator errors."""
    def __init__(self, message: str, code: str = "ORCHESTRATOR_ERROR", status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class StaleFencingTokenError(OrchestratorError):
    """Raised when an operation presents a fencing token strictly less than the active token."""
    def __init__(self, work_id: str, presented_token: int, active_token: int):
        super().__init__(
            f"Stale fencing token {presented_token} presented for work '{work_id}'. "
            f"Active token watermark is {active_token}. Operation rejected.",
            code="STALE_FENCING_TOKEN",
            status_code=409,
        )
        self.work_id = work_id
        self.presented_token = presented_token
        self.active_token = active_token


class InvalidTransitionError(OrchestratorError):
    """Raised when an illegal state machine transition is attempted."""
    def __init__(self, from_state: str, to_state: str, reason: str = ""):
        msg = f"Invalid state transition from '{from_state}' to '{to_state}'"
        if reason:
            msg += f": {reason}"
        super().__init__(msg, code="INVALID_STATE_TRANSITION", status_code=400)
        self.from_state = from_state
        self.to_state = to_state


class UnauthorizedTransitionError(OrchestratorError):
    """Raised when an actor lacks permission to trigger a specific transition."""
    def __init__(self, actor_role: str, to_state: str):
        super().__init__(
            f"Actor with role '{actor_role}' is not authorized to transition to '{to_state}'.",
            code="UNAUTHORIZED_TRANSITION",
            status_code=403,
        )
        self.actor_role = actor_role
        self.to_state = to_state


class LeaseExpiredError(OrchestratorError):
    """Raised when a worker attempts an action with an expired lease."""
    def __init__(self, work_id: str, holder_id: str):
        super().__init__(
            f"Lease on work '{work_id}' held by '{holder_id}' has expired.",
            code="LEASE_EXPIRED",
            status_code=410,
        )
        self.work_id = work_id
        self.holder_id = holder_id


class WorkNotFoundError(OrchestratorError):
    """Raised when a referenced Work entity does not exist."""
    def __init__(self, work_id: str):
        super().__init__(
            f"Work '{work_id}' not found.",
            code="WORK_NOT_FOUND",
            status_code=404,
        )
        self.work_id = work_id


class DependencyUnsatisfiedError(OrchestratorError):
    """Raised when work cannot proceed due to incomplete dependencies."""
    def __init__(self, work_id: str, unsatisfied_deps: list[str]):
        super().__init__(
            f"Work '{work_id}' has unsatisfied dependencies: {unsatisfied_deps}",
            code="DEPENDENCY_UNSATISFIED",
            status_code=412,
        )
        self.work_id = work_id
        self.unsatisfied_deps = unsatisfied_deps


class ConcurrencyConflictError(OrchestratorError):
    """Raised when an optimistic concurrency version check fails."""
    def __init__(self, entity_id: str, expected_version: int, actual_version: int):
        super().__init__(
            f"Optimistic lock conflict for entity '{entity_id}': expected version {expected_version}, got {actual_version}.",
            code="CONCURRENCY_CONFLICT",
            status_code=409,
        )


class IdempotencyConflictError(OrchestratorError):
    """Raised when an idempotency key is reused with mismatched payload."""
    def __init__(self, idempotency_key: str):
        super().__init__(
            f"Idempotency key '{idempotency_key}' was previously used with different request parameters.",
            code="IDEMPOTENCY_CONFLICT",
            status_code=422,
        )
