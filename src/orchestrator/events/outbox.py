"""Transactional outbox relayer for at-least-once event delivery."""

from typing import Callable, List, Optional, Awaitable
from orchestrator.domain.models import DomainEvent
from orchestrator.storage.interface import StorageBackend


class OutboxRelayer:
    """Reads pending outbox events from storage and dispatches them to external subscribers."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._subscribers: List[Callable[[DomainEvent], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[DomainEvent], Awaitable[None]]) -> None:
        """Registers an asynchronous event listener."""
        self._subscribers.append(callback)

    async def drain_once(self, limit: int = 100) -> int:
        """Fetches pending events, dispatches to subscribers, and marks applied."""
        events = await self.storage.fetch_pending_outbox(limit=limit)
        count = 0
        for evt in events:
            for sub in self._subscribers:
                await sub(evt)
            await self.storage.mark_outbox_applied(evt.id)
            count += 1
        return count
