"""Deterministic mock runtime adapter for testing, fault injection, and simulation."""

import asyncio
from typing import Any, Callable, Dict, Optional, Awaitable
from orchestrator.domain.models import DomainEvent, Artifact, utc_now
from orchestrator.domain.status import ExecutionStatus
from orchestrator.runtime.interface import (
    AgentRuntime,
    ExecutionContext,
    ExecutionHandle,
    ExecutionOutcome,
    Checkpoint,
)


class MockAgentRuntime(AgentRuntime):
    """Deterministic in-memory runtime adapter."""

    def __init__(
        self,
        default_status: ExecutionStatus = ExecutionStatus.COMPLETED,
        default_exit_code: int = 0,
        delay_seconds: float = 0.0,
        fail_with_error: Optional[str] = None,
    ):
        self.default_status = default_status
        self.default_exit_code = default_exit_code
        self.delay_seconds = delay_seconds
        self.fail_with_error = fail_with_error
        self.received_messages: list[str] = []
        self.running_handles: Dict[str, ExecutionContext] = {}

    async def start(
        self,
        context: ExecutionContext,
        event_sink: Optional[Callable[[DomainEvent], Awaitable[None]]] = None,
    ) -> ExecutionHandle:
        self.running_handles[context.execution_id] = context
        handle = ExecutionHandle(
            execution_id=context.execution_id,
            runtime_type="mock",
            process_id=99999,
        )
        if event_sink:
            evt = DomainEvent(
                type="io.orchestrator.execution.started",
                work_id=context.work_id,
                execution_id=context.execution_id,
                data={"attempt": context.attempt, "fencing_token": context.fencing_token},
            )
            await event_sink(evt)
        return handle

    async def send_message(self, handle: ExecutionHandle, message: str) -> None:
        self.received_messages.append(message)

    async def pause(self, handle: ExecutionHandle) -> Checkpoint:
        return Checkpoint(
            execution_id=handle.execution_id,
            step_index=1,
            state_payload={"mock": True},
        )

    async def resume(self, handle: ExecutionHandle, checkpoint: Checkpoint) -> None:
        pass

    async def cancel(self, handle: ExecutionHandle, grace_period_seconds: float = 15.0) -> None:
        if handle.execution_id in self.running_handles:
            del self.running_handles[handle.execution_id]

    async def wait(self, handle: ExecutionHandle) -> ExecutionOutcome:
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        if self.fail_with_error:
            return ExecutionOutcome(
                execution_id=handle.execution_id,
                status=ExecutionStatus.FAILED,
                exit_code=1,
                error_message=self.fail_with_error,
            )

        return ExecutionOutcome(
            execution_id=handle.execution_id,
            status=self.default_status,
            exit_code=self.default_exit_code,
            candidate_artifacts=[
                Artifact(
                    artifact_id="art-mock-1",
                    execution_id=handle.execution_id,
                    name="output.patch",
                    digest_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    storage_uri="mock://output.patch",
                    size_bytes=100,
                )
            ],
        )
