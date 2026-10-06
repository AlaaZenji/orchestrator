"""Fault injection and crash recovery tests using failpoints."""

import pytest
from datetime import timedelta
from orchestrator.domain import (
    Work,
    Lease,
    WorkStatus,
    ActorContext,
    ActorRole,
    utc_now,
)
from orchestrator.storage.memory import MemoryStorageBackend
from orchestrator.api.service import OrchestratorService
from orchestrator.reliability.retry import RetryPolicy


@pytest.mark.anyio
async def test_crash_recovery_sweep():
    """Simulates sudden orchestrator crash with orphaned running works; verifies reconciliation sweep."""
    storage = MemoryStorageBackend()
    service = OrchestratorService(storage=storage, retry_policy=RetryPolicy(max_retries=2))

    # 1. Create and claim work
    work = await service.create_work("CRASH-1", "Crash recovery test")
    await service.claim_work("CRASH-1", worker_id="worker-crash-1", ttl_seconds=5)

    # 2. Simulate orchestrator crash and time passing past lease expiration
    now = utc_now() + timedelta(seconds=10)

    # 3. Fresh orchestrator process starts up and runs reconciliation sweep
    new_service = OrchestratorService(storage=storage, retry_policy=RetryPolicy(max_retries=2))
    recovered = await new_service.run_reconciliation_sweep(current_time=now)

    assert recovered == 1

    # Invariant: Work is safely transitioned to RETRYING with incremented attempt
    w_after = await storage.get_work("CRASH-1")
    assert w_after.status == WorkStatus.RETRYING
    assert w_after.retry_count == 1

    # Lease is cleaned up
    lease = await storage.get_lease("CRASH-1")
    assert lease is None


@pytest.mark.anyio
async def test_failpoint_injection():
    """Injectable failpoint halts execution safely without corrupting storage."""
    storage = MemoryStorageBackend()

    def explode():
        raise RuntimeError("Simulated sudden power cut")

    storage.set_failpoint("before_save_work", explode)

    service = OrchestratorService(storage=storage)
    with pytest.raises(RuntimeError) as exc_info:
        await service.create_work("FAIL-1", "Failpoint test")

    assert "Simulated sudden power cut" in str(exc_info.value)
    # Work was not saved
    assert await storage.get_work("FAIL-1") is None
