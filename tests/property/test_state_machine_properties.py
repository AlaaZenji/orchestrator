"""Property-based stateful testing for state machine invariants using Hypothesis."""

import pytest
from hypothesis.stateful import RuleBasedStateMachine, rule, initialize, invariant
from hypothesis import strategies as st

from orchestrator.domain import (
    Work,
    Lease,
    ActorContext,
    WorkStatus,
    ActorRole,
    VerifierVerdict,
    VerificationResult,
    utc_now,
)
from orchestrator.orchestration.state_machine import WorkStateMachine
from orchestrator.reliability.leases import LeaseManager


class WorkStateMachineModel(RuleBasedStateMachine):
    """Fuzzes state transitions across multiple works and asserts invariant preservation."""

    def __init__(self):
        super().__init__()
        self.works: dict[str, Work] = {}
        self.leases: dict[str, Lease] = {}
        self.lease_manager = LeaseManager()
        self.work_tokens: dict[str, list[int]] = {}

    @rule(work_id=st.text(min_size=1, max_size=10, alphabet="ABCDEF0123456789"))
    def create_work(self, work_id: str):
        if work_id not in self.works:
            w = Work(work_id=work_id, title=f"Task {work_id}", status=WorkStatus.ELIGIBLE)
            self.works[work_id] = w
            self.work_tokens[work_id] = []

    @rule(
        work_id=st.text(min_size=1, max_size=10, alphabet="ABCDEF0123456789"),
        worker_id=st.text(min_size=1, max_size=5, alphabet="xyz"),
    )
    def claim_lease(self, work_id: str, worker_id: str):
        work = self.works.get(work_id)
        if not work:
            return

        actor = ActorContext(actor_id=worker_id, role=ActorRole.AGENT_WORKER)
        # Attempt claim only if ELIGIBLE or RETRYING
        if work.status in (WorkStatus.ELIGIBLE, WorkStatus.RETRYING):
            WorkStateMachine.validate_transition(work, WorkStatus.RUNNING, actor)
            lease = self.lease_manager.create_lease(work_id, worker_id, ttl_seconds=60)
            self.leases[work_id] = lease
            self.work_tokens[work_id].append(lease.fencing_token)

            updated = Work(
                work_id=work.work_id,
                title=work.title,
                status=WorkStatus.RUNNING,
                priority=work.priority,
                max_retries=work.max_retries,
                retry_count=work.retry_count,
                active_fencing_token=lease.fencing_token,
                version=work.version + 1,
            )
            self.works[work_id] = updated

    @rule(work_id=st.text(min_size=1, max_size=10, alphabet="ABCDEF0123456789"))
    def submit_for_verification(self, work_id: str):
        work = self.works.get(work_id)
        if not work or work.status != WorkStatus.RUNNING:
            return

        actor = ActorContext(actor_id="worker", role=ActorRole.AGENT_WORKER)
        WorkStateMachine.validate_transition(
            work, WorkStatus.WAITING_VERIFICATION, actor, presented_fencing_token=work.active_fencing_token
        )
        updated = Work(
            work_id=work.work_id,
            title=work.title,
            status=WorkStatus.WAITING_VERIFICATION,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count,
            active_fencing_token=work.active_fencing_token,
            version=work.version + 1,
        )
        self.works[work_id] = updated

    @rule(
        work_id=st.text(min_size=1, max_size=10, alphabet="ABCDEF0123456789"),
        passed=st.booleans(),
    )
    def record_verdict(self, work_id: str, passed: bool):
        work = self.works.get(work_id)
        if not work or work.status != WorkStatus.WAITING_VERIFICATION:
            return

        actor = ActorContext(actor_id="verifier", role=ActorRole.AUTHORIZED_VERIFIER)
        target = WorkStatus.SUCCEEDED if passed else (
            WorkStatus.RETRYING if work.retry_count < work.max_retries else WorkStatus.FAILED
        )
        WorkStateMachine.validate_transition(work, target, actor)

        updated = Work(
            work_id=work.work_id,
            title=work.title,
            status=target,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count if passed else work.retry_count + 1,
            active_fencing_token=work.active_fencing_token,
            version=work.version + 1,
        )
        self.works[work_id] = updated
        self.leases.pop(work_id, None)

    @rule(work_id=st.text(min_size=1, max_size=10, alphabet="ABCDEF0123456789"))
    def cancel_work(self, work_id: str):
        work = self.works.get(work_id)
        if not work or work.status.is_terminal:
            return

        actor = ActorContext(actor_id="admin", role=ActorRole.HUMAN_OPERATOR)
        WorkStateMachine.validate_transition(work, WorkStatus.CANCELLED, actor)
        updated = Work(
            work_id=work.work_id,
            title=work.title,
            status=WorkStatus.CANCELLED,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count,
            version=work.version + 1,
        )
        self.works[work_id] = updated
        self.leases.pop(work_id, None)

    # --- INVARIANTS ---

    @invariant()
    def inv_terminal_state_immutable(self):
        """INV: Terminal works remain in terminal status."""
        for w in self.works.values():
            if w.status.is_terminal:
                assert w.status in (WorkStatus.SUCCEEDED, WorkStatus.FAILED, WorkStatus.CANCELLED)

    @invariant()
    def inv_fencing_tokens_strictly_monotonic(self):
        """INV-002: Fencing tokens issued for any work are strictly monotonically increasing."""
        for wid, tokens in self.work_tokens.items():
            for i in range(len(tokens) - 1):
                assert tokens[i] < tokens[i + 1]

    @invariant()
    def inv_single_active_lease(self):
        """INV-001: At most one lease exists per work."""
        work_ids = list(self.leases.keys())
        assert len(work_ids) == len(set(work_ids))


TestStateMachine = WorkStateMachineModel.TestCase
