"""Deterministic Finite State Machine for Work and Execution lifecycles."""

from typing import Optional, Set, Tuple
from orchestrator.domain.status import WorkStatus, ExecutionStatus, ActorRole
from orchestrator.domain.models import Work, Execution, ActorContext
from orchestrator.domain.exceptions import (
    InvalidTransitionError,
    UnauthorizedTransitionError,
    StaleFencingTokenError,
)


class WorkStateMachine:
    """Enforces legal transitions, actor authorization, and invariants for Work."""

    # Set of strictly legal (from_status, to_status) tuples
    LEGAL_TRANSITIONS: Set[Tuple[WorkStatus, WorkStatus]] = {
        (WorkStatus.PENDING, WorkStatus.ELIGIBLE),
        (WorkStatus.PENDING, WorkStatus.CANCELLED),
        (WorkStatus.ELIGIBLE, WorkStatus.RUNNING),
        (WorkStatus.ELIGIBLE, WorkStatus.CANCELLED),
        (WorkStatus.RUNNING, WorkStatus.WAITING_VERIFICATION),
        (WorkStatus.RUNNING, WorkStatus.AWAITING_APPROVAL),
        (WorkStatus.RUNNING, WorkStatus.RETRYING),
        (WorkStatus.RUNNING, WorkStatus.FAILED),
        (WorkStatus.RUNNING, WorkStatus.CANCELLED),
        (WorkStatus.AWAITING_APPROVAL, WorkStatus.RUNNING),
        (WorkStatus.AWAITING_APPROVAL, WorkStatus.CANCELLED),
        (WorkStatus.WAITING_VERIFICATION, WorkStatus.SUCCEEDED),
        (WorkStatus.WAITING_VERIFICATION, WorkStatus.RETRYING),
        (WorkStatus.WAITING_VERIFICATION, WorkStatus.FAILED),
        (WorkStatus.WAITING_VERIFICATION, WorkStatus.CANCELLED),
        (WorkStatus.RETRYING, WorkStatus.ELIGIBLE),
        (WorkStatus.RETRYING, WorkStatus.CANCELLED),
    }

    @classmethod
    def validate_transition(
        cls,
        work: Work,
        to_status: WorkStatus,
        actor: ActorContext,
        presented_fencing_token: Optional[int] = None,
    ) -> None:
        """Validates that a transition is legal, authorized, and preserves all invariants."""

        # Invariant INV-002: Check fencing token FIRST if presented. A stale worker must be rejected immediately.
        if presented_fencing_token is not None and work.active_fencing_token is not None:
            if presented_fencing_token < work.active_fencing_token:
                raise StaleFencingTokenError(
                    work.work_id, presented_fencing_token, work.active_fencing_token
                )

        # Invariant: Terminal states are immutable
        if work.status.is_terminal:
            raise InvalidTransitionError(
                work.status.value,
                to_status.value,
                f"Work '{work.work_id}' is in terminal status '{work.status.value}' and cannot transition.",
            )

        # Check transition legality
        if (work.status, to_status) not in cls.LEGAL_TRANSITIONS:
            raise InvalidTransitionError(
                work.status.value,
                to_status.value,
                f"Transition from {work.status.value} to {to_status.value} is not legally permitted.",
            )

        # Invariant INV-004: Agent cannot self-transition to SUCCEEDED
        if to_status == WorkStatus.SUCCEEDED and actor.role == ActorRole.AGENT_WORKER:
            raise UnauthorizedTransitionError(actor.role.value, to_status.value)

        # Invariant: Agent cannot approve its own work
        if to_status == WorkStatus.RUNNING and work.status == WorkStatus.AWAITING_APPROVAL:
            if actor.role not in (ActorRole.HUMAN_OPERATOR, ActorRole.SYSTEM_RECONCILER):
                raise UnauthorizedTransitionError(actor.role.value, to_status.value)


class ExecutionStateMachine:
    """Enforces legal transitions for an individual Execution attempt."""

    LEGAL_TRANSITIONS: Set[Tuple[ExecutionStatus, ExecutionStatus]] = {
        (ExecutionStatus.STARTING, ExecutionStatus.RUNNING),
        (ExecutionStatus.STARTING, ExecutionStatus.FAILED),
        (ExecutionStatus.STARTING, ExecutionStatus.CANCELLED),
        (ExecutionStatus.RUNNING, ExecutionStatus.PAUSED),
        (ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED),
        (ExecutionStatus.RUNNING, ExecutionStatus.FAILED),
        (ExecutionStatus.RUNNING, ExecutionStatus.TIMED_OUT),
        (ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED),
        (ExecutionStatus.PAUSED, ExecutionStatus.RUNNING),
        (ExecutionStatus.PAUSED, ExecutionStatus.CANCELLED),
    }

    @classmethod
    def validate_transition(cls, execution: Execution, to_status: ExecutionStatus) -> None:
        if execution.status.is_terminal:
            raise InvalidTransitionError(
                execution.status.value,
                to_status.value,
                f"Execution '{execution.execution_id}' is in terminal status '{execution.status.value}'.",
            )
        if (execution.status, to_status) not in cls.LEGAL_TRANSITIONS:
            raise InvalidTransitionError(execution.status.value, to_status.value)
