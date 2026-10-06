"""Lease management and monotonic sequence fencing token coordination."""

from datetime import datetime, timedelta
from typing import Optional
from orchestrator.domain.models import Lease, utc_now
from orchestrator.domain.exceptions import StaleFencingTokenError, LeaseExpiredError


class LeaseManager:
    """Coordinates lease acquisition, renewal, and monotonic fencing."""

    def __init__(self, default_ttl_seconds: int = 180):
        self.default_ttl_seconds = default_ttl_seconds
        self._fencing_sequence: int = 0

    def next_fencing_token(self) -> int:
        """Returns a strictly monotonically increasing 64-bit integer token."""
        self._fencing_sequence += 1
        return self._fencing_sequence

    def create_lease(
        self,
        work_id: str,
        holder_id: str,
        ttl_seconds: Optional[int] = None,
        tenant_id: str = "default",
        fencing_token: Optional[int] = None,
    ) -> Lease:
        """Issues a new lease with an incremented fencing token."""
        token = fencing_token if fencing_token is not None else self.next_fencing_token()
        ttl = ttl_seconds or self.default_ttl_seconds
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl)
        return Lease(
            work_id=work_id,
            holder_id=holder_id,
            fencing_token=token,
            expires_at=expires_at,
            acquired_at=now,
            tenant_id=tenant_id,
        )

    def renew_lease(
        self,
        current_lease: Lease,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: Optional[int] = None,
    ) -> Lease:
        """Renews an existing lease without advancing the fencing token, verifying ownership."""
        now = utc_now()
        if current_lease.is_expired(now):
            raise LeaseExpiredError(current_lease.work_id, holder_id)

        if current_lease.holder_id != holder_id:
            raise LeaseExpiredError(
                current_lease.work_id,
                f"Holder mismatch: expected {current_lease.holder_id}, got {holder_id}",
            )

        if fencing_token != current_lease.fencing_token:
            raise StaleFencingTokenError(
                current_lease.work_id, fencing_token, current_lease.fencing_token
            )

        ttl = ttl_seconds or self.default_ttl_seconds
        return Lease(
            work_id=current_lease.work_id,
            holder_id=holder_id,
            fencing_token=current_lease.fencing_token,
            expires_at=now + timedelta(seconds=ttl),
            acquired_at=current_lease.acquired_at,
            tenant_id=current_lease.tenant_id,
        )
