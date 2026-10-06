"""High-level Orchestrator Service integrating state machines, leases, fencing, and events."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.domain.models import (
    Work,
    Execution,
    Lease,
    Artifact,
    VerificationResult,
    ApprovalRequest,
    DomainEvent,
    ActorContext,
    utc_now,
)
from orchestrator.domain.status import (
    WorkStatus,
    ExecutionStatus,
    ActorRole,
    VerifierVerdict,
)
from orchestrator.domain.exceptions import (
    WorkNotFoundError,
    StaleFencingTokenError,
    LeaseExpiredError,
    InvalidTransitionError,
    UnauthorizedTransitionError,
)
from orchestrator.orchestration.state_machine import WorkStateMachine, ExecutionStateMachine
from orchestrator.orchestration.dependency import DependencyResolver
from orchestrator.orchestration.scheduler import Scheduler
from orchestrator.reliability.leases import LeaseManager
from orchestrator.reliability.fencing import FencedGateway
from orchestrator.reliability.idempotency import IdempotencyManager
from orchestrator.reliability.watchdog import LeaseWatchdog
from orchestrator.reliability.reconciliation import CrashReconciler
from orchestrator.reliability.retry import RetryPolicy
from orchestrator.storage.interface import StorageBackend
from orchestrator.runtime.interface import AgentRuntime, ExecutionContext, ExecutionOutcome
from orchestrator.sandbox.interface import SandboxProvider


class OrchestratorService:
    """The central control plane coordinating all durable agent orchestration operations."""

    def __init__(
        self,
        storage: StorageBackend,
        runtime: Optional[AgentRuntime] = None,
        sandbox: Optional[SandboxProvider] = None,
        retry_policy: Optional[RetryPolicy] = None,
        lease_ttl_seconds: int = 180,
    ):
        self.storage = storage
        self.runtime = runtime
        self.sandbox = sandbox
        self.retry_policy = retry_policy or RetryPolicy()
        self.lease_manager = LeaseManager(default_ttl_seconds=lease_ttl_seconds)
        self.scheduler = Scheduler()
        self.idempotency = IdempotencyManager()

    # --- Work Creation ---

    async def create_work(
        self,
        work_id: str,
        title: str,
        description: str = "",
        priority: int = 0,
        dependencies: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        inputs: Optional[Dict[str, Any]] = None,
        tenant_id: str = "default",
        actor: Optional[ActorContext] = None,
        idempotency_key: Optional[str] = None,
    ) -> Work:
        act = actor or ActorContext(actor_id="system", role=ActorRole.HUMAN_OPERATOR)

        if idempotency_key:
            rec = self.idempotency.check_or_record(
                idempotency_key, "create_work", {"work_id": work_id, "title": title}
            )
            if rec:
                existing = await self.storage.get_work(work_id)
                if existing:
                    return existing

        existing = await self.storage.get_work(work_id)
        if existing:
            return existing

        initial_status = WorkStatus.ELIGIBLE if not dependencies else WorkStatus.PENDING
        work = Work(
            work_id=work_id,
            title=title,
            description=description,
            status=initial_status,
            priority=priority,
            dependencies=dependencies,
            labels=labels,
            inputs=inputs or {},
            tenant_id=tenant_id,
        )
        await self.storage.save_work(work)

        evt = DomainEvent(
            type="io.orchestrator.work.created",
            work_id=work_id,
            data={"title": title, "status": initial_status.value, "priority": priority},
        )
        await self.storage.append_event(evt)

        if idempotency_key:
            self.idempotency.save_response(
                idempotency_key, "create_work", {"work_id": work_id, "title": title}, 201, work.to_dict()
            )

        return work

    # --- Lease Claims ---

    async def claim_work(
        self,
        work_id: str,
        worker_id: str,
        ttl_seconds: Optional[int] = None,
        actor: Optional[ActorContext] = None,
    ) -> Tuple[Work, Lease]:
        act = actor or ActorContext(actor_id=worker_id, role=ActorRole.AGENT_WORKER)
        work = await self.storage.get_work(work_id)
        if not work:
            raise WorkNotFoundError(work_id)

        # Enforce legal transition from ELIGIBLE to RUNNING
        WorkStateMachine.validate_transition(work, WorkStatus.RUNNING, act)

        # Issue new monotonic fencing token
        token = await self.storage.next_fencing_token()
        lease = self.lease_manager.create_lease(
            work_id=work_id,
            holder_id=worker_id,
            ttl_seconds=ttl_seconds,
            tenant_id=work.tenant_id,
            fencing_token=token,
        )

        exec_id = f"exec-{work_id}-{work.retry_count + 1}"
        execution = Execution(
            execution_id=exec_id,
            work_id=work_id,
            attempt=work.retry_count + 1,
            fencing_token=token,
            status=ExecutionStatus.RUNNING,
            worker_id=worker_id,
        )

        updated_work = Work(
            work_id=work.work_id,
            title=work.title,
            description=work.description,
            status=WorkStatus.RUNNING,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count,
            timeout_seconds=work.timeout_seconds,
            heartbeat_timeout_seconds=work.heartbeat_timeout_seconds,
            dependencies=work.dependencies,
            labels=work.labels,
            inputs=work.inputs,
            active_fencing_token=token,
            active_execution_id=exec_id,
            tenant_id=work.tenant_id,
            version=work.version + 1,
            created_at=work.created_at,
            updated_at=utc_now(),
        )

        await self.storage.save_work(updated_work)
        await self.storage.save_lease(lease)
        await self.storage.save_execution(execution)

        evt = DomainEvent(
            type="io.orchestrator.lease.claimed",
            work_id=work_id,
            execution_id=exec_id,
            data={"worker_id": worker_id, "fencing_token": token, "expires_at": lease.expires_at.isoformat()},
        )
        await self.storage.append_event(evt)
        return updated_work, lease

    # --- Heartbeat Renewal ---

    async def heartbeat(
        self,
        work_id: str,
        worker_id: str,
        fencing_token: int,
        ttl_seconds: Optional[int] = None,
    ) -> Lease:
        lease = await self.storage.get_lease(work_id)
        if not lease:
            raise LeaseExpiredError(work_id, worker_id)

        renewed = self.lease_manager.renew_lease(
            current_lease=lease,
            holder_id=worker_id,
            fencing_token=fencing_token,
            ttl_seconds=ttl_seconds,
        )
        await self.storage.save_lease(renewed)
        return renewed

    # --- Verification & Promotion ---

    async def submit_for_verification(
        self,
        work_id: str,
        fencing_token: int,
        actor: ActorContext,
        candidate_artifacts: Optional[List[Artifact]] = None,
    ) -> Work:
        work = await self.storage.get_work(work_id)
        if not work:
            raise WorkNotFoundError(work_id)

        WorkStateMachine.validate_transition(
            work, WorkStatus.WAITING_VERIFICATION, actor, presented_fencing_token=fencing_token
        )

        updated_work = Work(
            work_id=work.work_id,
            title=work.title,
            description=work.description,
            status=WorkStatus.WAITING_VERIFICATION,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count,
            timeout_seconds=work.timeout_seconds,
            heartbeat_timeout_seconds=work.heartbeat_timeout_seconds,
            dependencies=work.dependencies,
            labels=work.labels,
            inputs=work.inputs,
            active_fencing_token=work.active_fencing_token,
            active_execution_id=work.active_execution_id,
            tenant_id=work.tenant_id,
            version=work.version + 1,
            created_at=work.created_at,
            updated_at=utc_now(),
        )
        await self.storage.save_work(updated_work)

        evt = DomainEvent(
            type="io.orchestrator.execution.completed",
            work_id=work_id,
            execution_id=work.active_execution_id,
            data={"fencing_token": fencing_token},
        )
        await self.storage.append_event(evt)
        return updated_work

    async def record_verification_verdict(
        self,
        work_id: str,
        result: VerificationResult,
        actor: ActorContext,
    ) -> Work:
        work = await self.storage.get_work(work_id)
        if not work:
            raise WorkNotFoundError(work_id)

        target_status = WorkStatus.SUCCEEDED if result.passed else (
            WorkStatus.RETRYING if self.retry_policy.should_retry(work.retry_count) else WorkStatus.FAILED
        )

        WorkStateMachine.validate_transition(work, target_status, actor)

        new_retry = work.retry_count if target_status == WorkStatus.SUCCEEDED else work.retry_count + 1
        updated_work = Work(
            work_id=work.work_id,
            title=work.title,
            description=work.description,
            status=target_status,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=new_retry,
            timeout_seconds=work.timeout_seconds,
            heartbeat_timeout_seconds=work.heartbeat_timeout_seconds,
            dependencies=work.dependencies,
            labels=work.labels,
            inputs=work.inputs,
            active_fencing_token=work.active_fencing_token,
            active_execution_id=work.active_execution_id,
            tenant_id=work.tenant_id,
            version=work.version + 1,
            created_at=work.created_at,
            updated_at=utc_now(),
        )

        await self.storage.save_work(updated_work)
        await self.storage.release_lease(work_id)

        evt_type = "io.orchestrator.work.succeeded" if target_status == WorkStatus.SUCCEEDED else "io.orchestrator.work.failed"
        evt = DomainEvent(
            type=evt_type,
            work_id=work_id,
            execution_id=work.active_execution_id,
            data={"verifier": result.verifier_name, "passed": result.passed, "summary": result.summary},
        )
        await self.storage.append_event(evt)
        return updated_work

    # --- Human Approvals ---

    async def approve_work(
        self,
        work_id: str,
        actor: ActorContext,
    ) -> Work:
        work = await self.storage.get_work(work_id)
        if not work:
            raise WorkNotFoundError(work_id)

        WorkStateMachine.validate_transition(work, WorkStatus.RUNNING, actor)

        updated_work = Work(
            work_id=work.work_id,
            title=work.title,
            description=work.description,
            status=WorkStatus.RUNNING,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count,
            timeout_seconds=work.timeout_seconds,
            heartbeat_timeout_seconds=work.heartbeat_timeout_seconds,
            dependencies=work.dependencies,
            labels=work.labels,
            inputs=work.inputs,
            active_fencing_token=work.active_fencing_token,
            active_execution_id=work.active_execution_id,
            tenant_id=work.tenant_id,
            version=work.version + 1,
            created_at=work.created_at,
            updated_at=utc_now(),
        )
        await self.storage.save_work(updated_work)

        evt = DomainEvent(
            type="io.orchestrator.approval.granted",
            work_id=work_id,
            data={"approver": actor.actor_id},
        )
        await self.storage.append_event(evt)
        return updated_work

    # --- Cancellation ---

    async def cancel_work(
        self,
        work_id: str,
        reason: str,
        actor: ActorContext,
    ) -> Work:
        work = await self.storage.get_work(work_id)
        if not work:
            raise WorkNotFoundError(work_id)

        WorkStateMachine.validate_transition(work, WorkStatus.CANCELLED, actor)

        updated_work = Work(
            work_id=work.work_id,
            title=work.title,
            description=work.description,
            status=WorkStatus.CANCELLED,
            priority=work.priority,
            max_retries=work.max_retries,
            retry_count=work.retry_count,
            timeout_seconds=work.timeout_seconds,
            heartbeat_timeout_seconds=work.heartbeat_timeout_seconds,
            dependencies=work.dependencies,
            labels=work.labels,
            inputs=work.inputs,
            active_fencing_token=work.active_fencing_token,
            active_execution_id=work.active_execution_id,
            tenant_id=work.tenant_id,
            version=work.version + 1,
            created_at=work.created_at,
            updated_at=utc_now(),
        )
        await self.storage.save_work(updated_work)
        await self.storage.release_lease(work_id)

        evt = DomainEvent(
            type="io.orchestrator.work.cancelled",
            work_id=work_id,
            data={"reason": reason, "cancelled_by": actor.actor_id},
        )
        await self.storage.append_event(evt)
        return updated_work

    # --- Crash Reconciliation Sweep ---

    async def run_reconciliation_sweep(self, current_time: Optional[datetime] = None) -> int:
        """Sweeps active leases and stalled works, recovering cluster consistency."""
        now = current_time or utc_now()
        works = await self.storage.list_works()
        leases = await self.storage.list_active_leases()

        stalled = LeaseWatchdog.identify_stalled_works(works, leases, now)
        recovered_count = 0

        for w in stalled:
            target_status, new_retry, reason = CrashReconciler.reconcile_stalled_work(
                w, now, self.retry_policy
            )
            updated = Work(
                work_id=w.work_id,
                title=w.title,
                description=w.description,
                status=target_status,
                priority=w.priority,
                max_retries=w.max_retries,
                retry_count=new_retry,
                timeout_seconds=w.timeout_seconds,
                heartbeat_timeout_seconds=w.heartbeat_timeout_seconds,
                dependencies=w.dependencies,
                labels=w.labels,
                inputs=w.inputs,
                active_fencing_token=w.active_fencing_token,
                active_execution_id=w.active_execution_id,
                tenant_id=w.tenant_id,
                version=w.version + 1,
                created_at=w.created_at,
                updated_at=now,
            )
            await self.storage.save_work(updated)
            await self.storage.release_lease(w.work_id)

            evt = DomainEvent(
                type="io.orchestrator.lease.expired",
                work_id=w.work_id,
                data={"reason": reason, "new_status": target_status.value},
            )
            await self.storage.append_event(evt)
            recovered_count += 1

        return recovered_count
