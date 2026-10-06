"""Deterministic fair-share priority scheduler."""

from typing import Optional, List
from datetime import datetime
from orchestrator.domain.models import Work, Lease, ActorContext, utc_now
from orchestrator.domain.status import WorkStatus, ActorRole
from orchestrator.orchestration.dependency import DependencyResolver


class Scheduler:
    """Selects eligible work and manages concurrent worker dispatch."""

    def __init__(self, max_concurrent_per_tenant: int = 10):
        self.max_concurrent_per_tenant = max_concurrent_per_tenant

    def select_next_work(
        self,
        candidate_works: List[Work],
        active_leases: List[Lease],
        tenant_id: str = "default",
    ) -> Optional[Work]:
        """Selects the highest-priority eligible work adhering to tenant quotas."""

        # Count active works for this tenant
        active_count = sum(1 for l in active_leases if l.tenant_id == tenant_id)
        if active_count >= self.max_concurrent_per_tenant:
            return None

        # Filter candidate works in ELIGIBLE status
        eligible = [
            w for w in candidate_works
            if w.status == WorkStatus.ELIGIBLE and w.tenant_id == tenant_id
        ]

        if not eligible:
            return None

        # Order by priority descending, then created_at ascending (FIFO for equal priority)
        eligible.sort(key=lambda w: (-w.priority, w.created_at))
        return eligible[0]
