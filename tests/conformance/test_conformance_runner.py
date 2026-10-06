"""Language-neutral conformance test runner executing declarative YAML scenarios."""

import pytest
import yaml
from pathlib import Path
from orchestrator.domain import (
    Work,
    WorkStatus,
    ActorContext,
    ActorRole,
    VerifierVerdict,
    VerificationResult,
    OrchestratorError,
)
from orchestrator.storage.memory import MemoryStorageBackend
from orchestrator.api.service import OrchestratorService
from orchestrator.orchestration.state_machine import WorkStateMachine


SCENARIOS_DIR = Path(__file__).parent.parent.parent / "conformance" / "scenarios"


@pytest.mark.anyio
@pytest.mark.parametrize("scenario_file", sorted(SCENARIOS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
async def test_run_conformance_scenario(scenario_file: Path):
    """Executes a declarative YAML conformance scenario against the reference orchestrator implementation."""
    content = scenario_file.read_text(encoding="utf-8")
    scenario_data = yaml.safe_load(content)

    storage = MemoryStorageBackend()
    service = OrchestratorService(storage=storage)

    for idx, step in enumerate(scenario_data.get("steps", [])):
        action = step.get("action")
        params = step.get("params", {})
        expected_status = step.get("expect", {}).get("status")
        expected_token = step.get("expect", {}).get("fencing_token")
        expected_error = step.get("expect_error")

        try:
            if action == "create_work":
                deps = tuple(params.get("dependencies", []))
                w = await service.create_work(
                    work_id=params["work_id"],
                    title=params["title"],
                    dependencies=deps,
                )
                if expected_status:
                    assert w.status.value == expected_status

            elif action == "claim_work":
                w, lease = await service.claim_work(
                    work_id=params["work_id"],
                    worker_id=params["worker_id"],
                )
                if expected_status:
                    assert w.status.value == expected_status
                if expected_token:
                    assert lease.fencing_token == expected_token

            elif action == "simulate_crash_and_requeue":
                # Simulate release of lease and requeueing to ELIGIBLE
                w = await storage.get_work(params["work_id"])
                await storage.release_lease(params["work_id"])
                w_requeued = Work(
                    work_id=w.work_id,
                    title=w.title,
                    status=WorkStatus.ELIGIBLE,
                    retry_count=w.retry_count + 1,
                    active_fencing_token=w.active_fencing_token,
                    version=w.version + 1,
                )
                await storage.save_work(w_requeued)
                if expected_status:
                    assert w_requeued.status.value == expected_status

            elif action == "submit_for_verification":
                actor_id = params.get("actor_id", "worker")
                actor = ActorContext(actor_id=actor_id, role=ActorRole.AGENT_WORKER)
                f_token = params.get("fencing_token")
                w = await service.submit_for_verification(
                    params["work_id"],
                    fencing_token=f_token,
                    actor=actor,
                )
                if expected_status:
                    assert w.status.value == expected_status

            elif action == "record_verdict":
                actor = ActorContext(actor_id="verifier", role=ActorRole.AUTHORIZED_VERIFIER)
                w = await storage.get_work(params["work_id"])
                verdict = VerificationResult(
                    verification_id="v-conf",
                    execution_id=w.active_execution_id or "exec-conf",
                    verifier_name="conformance-verifier",
                    verdict=VerifierVerdict.PASSED if params.get("passed") else VerifierVerdict.FAILED,
                    passed=bool(params.get("passed")),
                    summary="Conformance verdict",
                )
                res = await service.record_verification_verdict(params["work_id"], verdict, actor)
                if expected_status:
                    assert res.status.value == expected_status

            elif action == "request_approval":
                w = await storage.get_work(params["work_id"])
                actor = ActorContext(actor_id="policy", role=ActorRole.SYSTEM_RECONCILER)
                WorkStateMachine.validate_transition(w, WorkStatus.AWAITING_APPROVAL, actor)
                updated = Work(
                    work_id=w.work_id,
                    title=w.title,
                    status=WorkStatus.AWAITING_APPROVAL,
                    active_fencing_token=w.active_fencing_token,
                    version=w.version + 1,
                )
                await storage.save_work(updated)
                if expected_status:
                    assert updated.status.value == expected_status

            elif action == "attempt_agent_self_approval":
                actor = ActorContext(actor_id=params["actor_id"], role=ActorRole.AGENT_WORKER)
                await service.approve_work(params["work_id"], actor)

            elif action == "human_approve":
                actor = ActorContext(actor_id=params["actor_id"], role=ActorRole.HUMAN_OPERATOR)
                res = await service.approve_work(params["work_id"], actor)
                if expected_status:
                    assert res.status.value == expected_status

            else:
                pytest.fail(f"Unknown action: {action}")

            if expected_error:
                pytest.fail(f"Step {idx} expected error '{expected_error}' but succeeded.")

        except OrchestratorError as err:
            if not expected_error:
                raise
            assert err.code == expected_error, f"Expected error code '{expected_error}', got '{err.code}'"
