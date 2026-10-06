"""Abstract Storage Backend interface defining persistence, leases, and events."""

from abc import ABC, abstractmethod
from typing import Callable, List, Optional
from orchestrator.domain.models import Work, Execution, Lease, DomainEvent, Artifact


class StorageBackend(ABC):
    """Abstract persistence interface for the orchestrator."""

    # --- Work Operations ---

    @abstractmethod
    async def get_work(self, work_id: str) -> Optional[Work]:
        ...

    @abstractmethod
    async def save_work(self, work: Work) -> None:
        ...

    @abstractmethod
    async def list_works(self, tenant_id: str = "default") -> List[Work]:
        ...

    # --- Execution Operations ---

    @abstractmethod
    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        ...

    @abstractmethod
    async def save_execution(self, execution: Execution) -> None:
        ...

    @abstractmethod
    async def list_executions(self, work_id: str) -> List[Execution]:
        ...

    # --- Lease & Fencing Operations ---

    @abstractmethod
    async def get_lease(self, work_id: str) -> Optional[Lease]:
        ...

    @abstractmethod
    async def save_lease(self, lease: Lease) -> None:
        ...

    @abstractmethod
    async def release_lease(self, work_id: str) -> None:
        ...

    @abstractmethod
    async def list_active_leases(self, tenant_id: str = "default") -> List[Lease]:
        ...

    @abstractmethod
    async def next_fencing_token(self) -> int:
        """Returns a strictly monotonic integer sequence number."""
        ...

    # --- Events & Outbox Operations ---

    @abstractmethod
    async def append_event(self, event: DomainEvent) -> None:
        ...

    @abstractmethod
    async def list_events_for_work(self, work_id: str) -> List[DomainEvent]:
        ...

    @abstractmethod
    async def fetch_pending_outbox(self, limit: int = 100) -> List[DomainEvent]:
        ...

    @abstractmethod
    async def mark_outbox_applied(self, event_id: str) -> None:
        ...

    # --- Testing & Chaos Failpoints ---

    @abstractmethod
    def set_failpoint(self, name: str, hook: Optional[Callable[[], None]]) -> None:
        """Configures an injectable failure hook for fault-injection testing."""
        ...
