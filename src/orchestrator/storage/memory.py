"""In-memory storage backend for fast testing, property fuzzing, and simulations."""

import asyncio
from typing import Callable, Dict, List, Optional
from orchestrator.domain.models import Work, Execution, Lease, DomainEvent
from orchestrator.storage.interface import StorageBackend


class MemoryStorageBackend(StorageBackend):
    """Thread-safe in-memory storage backend."""

    def __init__(self):
        self._works: Dict[str, Work] = {}
        self._executions: Dict[str, Execution] = {}
        self._leases: Dict[str, Lease] = {}
        self._events: List[DomainEvent] = []
        self._outbox_applied: set[str] = set()
        self._fencing_seq: int = 0
        self._failpoints: Dict[str, Callable[[], None]] = {}
        self._lock = asyncio.Lock()

    def set_failpoint(self, name: str, hook: Optional[Callable[[], None]]) -> None:
        if hook is None:
            self._failpoints.pop(name, None)
        else:
            self._failpoints[name] = hook

    def _trigger_failpoint(self, name: str) -> None:
        if name in self._failpoints:
            self._failpoints[name]()

    async def get_work(self, work_id: str) -> Optional[Work]:
        async with self._lock:
            return self._works.get(work_id)

    async def save_work(self, work: Work) -> None:
        self._trigger_failpoint("before_save_work")
        async with self._lock:
            self._works[work.work_id] = work
        self._trigger_failpoint("after_save_work")

    async def list_works(self, tenant_id: str = "default") -> List[Work]:
        async with self._lock:
            return [w for w in self._works.values() if w.tenant_id == tenant_id]

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        async with self._lock:
            return self._executions.get(execution_id)

    async def save_execution(self, execution: Execution) -> None:
        async with self._lock:
            self._executions[execution.execution_id] = execution

    async def list_executions(self, work_id: str) -> List[Execution]:
        async with self._lock:
            return [e for e in self._executions.values() if e.work_id == work_id]

    async def get_lease(self, work_id: str) -> Optional[Lease]:
        async with self._lock:
            return self._leases.get(work_id)

    async def save_lease(self, lease: Lease) -> None:
        async with self._lock:
            self._leases[lease.work_id] = lease

    async def release_lease(self, work_id: str) -> None:
        async with self._lock:
            self._leases.pop(work_id, None)

    async def list_active_leases(self, tenant_id: str = "default") -> List[Lease]:
        async with self._lock:
            return [l for l in self._leases.values() if l.tenant_id == tenant_id]

    async def next_fencing_token(self) -> int:
        async with self._lock:
            self._fencing_seq += 1
            return self._fencing_seq

    async def append_event(self, event: DomainEvent) -> None:
        self._trigger_failpoint("before_append_event")
        async with self._lock:
            self._events.append(event)
        self._trigger_failpoint("after_append_event")

    async def list_events_for_work(self, work_id: str) -> List[DomainEvent]:
        async with self._lock:
            return [e for e in self._events if e.work_id == work_id]

    async def fetch_pending_outbox(self, limit: int = 100) -> List[DomainEvent]:
        async with self._lock:
            pending = [e for e in self._events if e.id not in self._outbox_applied]
            return pending[:limit]

    async def mark_outbox_applied(self, event_id: str) -> None:
        async with self._lock:
            self._outbox_applied.add(event_id)
