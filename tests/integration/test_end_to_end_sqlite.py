"""Integration tests for end-to-end SQLite workflow execution and outbox relayer."""

import pytest
from pathlib import Path
from orchestrator.domain import (
    Work,
    WorkStatus,
    ActorContext,
    ActorRole,
    VerifierVerdict,
    VerificationResult,
)
from orchestrator.storage.sqlite import SQLiteStorageBackend
from orchestrator.api.service import OrchestratorService
from orchestrator.orchestration.dependency import DependencyResolver
from orchestrator.events.outbox import OutboxRelayer


@pytest.mark.anyio
async def test_end_to_end_dag_workflow_sqlite(tmp_path: Path):
    """End-to-end execution of a 2-node DAG using SQLite backend and transactional outbox."""
    db_file = tmp_path / "test_state.db"
    storage = SQLiteStorageBackend(db_file)
    service = OrchestratorService(storage=storage)

    # 1. Create two works: Task 1 (no deps), Task 2 (depends on Task 1)
    w1 = await service.create_work("DAG-1", "Build foundation module")
    w2 = await service.create_work("DAG-2", "Build dependent feature", dependencies=("DAG-1",))

    assert w1.status == WorkStatus.ELIGIBLE
    assert w2.status == WorkStatus.PENDING

    # 2. Worker claims Task 1
    worker_ctx = ActorContext(actor_id="worker-1", role=ActorRole.AGENT_WORKER)
    claimed_w1, lease_1 = await service.claim_work("DAG-1", worker_id="worker-1")
    assert claimed_w1.status == WorkStatus.RUNNING
    assert lease_1.fencing_token >= 1

    # 3. Worker submits Task 1 for verification
    waiting_w1 = await service.submit_for_verification("DAG-1", lease_1.fencing_token, worker_ctx)
    assert waiting_w1.status == WorkStatus.WAITING_VERIFICATION

    # 4. Verifier passes Task 1
    verifier_ctx = ActorContext(actor_id="ci-bot", role=ActorRole.AUTHORIZED_VERIFIER)
    verdict = VerificationResult(
        verification_id="v-1",
        execution_id=claimed_w1.active_execution_id,
        verifier_name="pytest",
        verdict=VerifierVerdict.PASSED,
        passed=True,
        summary="All tests passed",
    )
    succeeded_w1 = await service.record_verification_verdict("DAG-1", verdict, verifier_ctx)
    assert succeeded_w1.status == WorkStatus.SUCCEEDED

    # 5. Dependency resolution: Task 2 now becomes ELIGIBLE
    all_works = await storage.list_works()
    eligible = DependencyResolver.compute_eligible_works(all_works)
    assert len(eligible) == 1
    assert eligible[0].work_id == "DAG-2"

    # Advance Task 2 to ELIGIBLE in DB
    w2_eligible = Work(
        work_id="DAG-2",
        title="Build dependent feature",
        status=WorkStatus.ELIGIBLE,
        dependencies=("DAG-1",),
        version=eligible[0].version + 1,
    )
    await storage.save_work(w2_eligible)

    # 6. Worker claims Task 2
    claimed_w2, lease_2 = await service.claim_work("DAG-2", worker_id="worker-2")
    assert claimed_w2.status == WorkStatus.RUNNING
    assert lease_2.fencing_token > lease_1.fencing_token

    # 7. Verify outbox relayer drained all events
    relayer = OutboxRelayer(storage)
    captured_events = []
    async def capture(evt):
        captured_events.append(evt.type)

    relayer.subscribe(capture)
    drained = await relayer.drain_once(limit=50)

    assert drained > 0
    assert "io.orchestrator.work.created" in captured_events
    assert "io.orchestrator.lease.claimed" in captured_events
    assert "io.orchestrator.work.succeeded" in captured_events
