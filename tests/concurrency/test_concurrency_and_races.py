"""Concurrency and race condition tests."""

import asyncio
import pytest
from datetime import timedelta
from orchestrator.domain import (
    Work,
    WorkStatus,
    ActorContext,
    ActorRole,
    StaleFencingTokenError,
    InvalidTransitionError,
    utc_now,
)
from orchestrator.storage.memory import MemoryStorageBackend
from orchestrator.api.service import OrchestratorService


@pytest.mark.anyio
async def test_simultaneous_claims_race():
    """50 concurrent workers race to claim the same work; exactly one succeeds."""
    storage = MemoryStorageBackend()
    service = OrchestratorService(storage=storage)

    work = await service.create_work("RACE-1", "Concurrent claim test")

    successful_claims = []
    failed_claims = []

    async def attempt_claim(worker_id: str):
        try:
            w, lease = await service.claim_work("RACE-1", worker_id=worker_id)
            successful_claims.append((worker_id, lease.fencing_token))
        except (InvalidTransitionError, Exception) as e:
            failed_claims.append((worker_id, str(e)))

    # Launch 50 concurrent workers
    tasks = [attempt_claim(f"worker-{i}") for i in range(50)]
    await asyncio.gather(*tasks)

    # Invariant: Exactly one worker holds the claim
    assert len(successful_claims) == 1
    assert len(failed_claims) == 49

    # Verify authoritative state
    final_work = await storage.get_work("RACE-1")
    assert final_work.status == WorkStatus.RUNNING
    assert final_work.active_fencing_token == successful_claims[0][1]


@pytest.mark.anyio
async def test_stale_worker_rejection():
    """Worker A gets token 1, pauses. Worker B gets token 2 and completes. Worker A write is rejected."""
    storage = MemoryStorageBackend()
    service = OrchestratorService(storage=storage)

    # 1. Create work and Worker A claims it
    await service.create_work("STALE-1", "Stale fencing test")
    w1, lease_a = await service.claim_work("STALE-1", worker_id="worker-A")
    token_a = lease_a.fencing_token

    # 2. Worker A simulates pause. Watchdog/reconciliation sweeps and marks RETRYING -> ELIGIBLE
    await service.storage.release_lease("STALE-1")
    w_retry = Work(
        work_id="STALE-1",
        title="Stale fencing test",
        status=WorkStatus.ELIGIBLE,
        retry_count=1,
        active_fencing_token=token_a,
    )
    await service.storage.save_work(w_retry)

    # 3. Worker B claims work -> gets higher token
    w2, lease_b = await service.claim_work("STALE-1", worker_id="worker-B")
    token_b = lease_b.fencing_token
    assert token_b > token_a

    # 4. Worker B completes successfully
    actor_b = ActorContext(actor_id="worker-B", role=ActorRole.AGENT_WORKER)
    await service.submit_for_verification("STALE-1", token_b, actor_b)

    # 5. Stale Worker A wakes up and attempts submission with token_a
    actor_a = ActorContext(actor_id="worker-A", role=ActorRole.AGENT_WORKER)
    with pytest.raises(StaleFencingTokenError) as exc_info:
        await service.submit_for_verification("STALE-1", token_a, actor_a)

    assert exc_info.value.presented_token == token_a
    assert exc_info.value.active_token == token_b
