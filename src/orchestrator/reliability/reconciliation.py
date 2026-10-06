"""Crash recovery reconciler restoring cluster consistency after restarts."""

from datetime import datetime
from typing import List, Tuple
from orchestrator.domain.models import Work, Lease, ActorContext, utc_now
from orchestrator.domain.status import WorkStatus, ActorRole
from orchestrator.orchestration.state_machine import WorkStateMachine
from orchestrator.reliability.retry import RetryPolicy


class CrashReconciler:
    """Restores consistent state following process restarts or worker drops."""

    @staticmethod
    def reconcile_stalled_work(
        work: Work,
        current_time: datetime,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> Tuple[WorkStatus, int, str]:
        """Evaluates a stalled work and determines its recovery transition.
        Returns (target_status, new_retry_count, reason).
        """
        actor = ActorContext(actor_id="reconciler", role=ActorRole.SYSTEM_RECONCILER)

        if retry_policy.should_retry(work.retry_count):
            target_status = WorkStatus.RETRYING
            new_retry_count = work.retry_count + 1
            reason = f"Worker lease expired. Attempt {new_retry_count} scheduled."
        else:
            target_status = WorkStatus.FAILED
            new_retry_count = work.retry_count
            reason = f"Worker lease expired and max retries ({work.max_retries}) exceeded."

        # Validate that transition is legal
        WorkStateMachine.validate_transition(work, target_status, actor)
        return target_status, new_retry_count, reason
