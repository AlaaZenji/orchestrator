"""Watchdog for lease expiration and heartbeat monitoring."""

from datetime import datetime
from typing import List, Tuple
from orchestrator.domain.models import Lease, Work, utc_now
from orchestrator.domain.status import WorkStatus


class LeaseWatchdog:
    """Detects expired leases and identifies stalled executions."""

    @staticmethod
    def find_expired_leases(leases: List[Lease], current_time: datetime) -> List[Lease]:
        """Returns all leases that have expired relative to current_time."""
        return [l for l in leases if l.is_expired(current_time)]

    @staticmethod
    def identify_stalled_works(
        works: List[Work],
        active_leases: List[Lease],
        current_time: datetime,
    ) -> List[Work]:
        """Identifies works in active status whose leases have expired or are missing."""
        lease_map = {l.work_id: l for l in active_leases}
        stalled: List[Work] = []
        for w in works:
            if w.status == WorkStatus.RUNNING:
                lease = lease_map.get(w.work_id)
                if not lease or lease.is_expired(current_time):
                    stalled.append(w)
        return stalled
