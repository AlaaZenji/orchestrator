"""Claude Code runtime adapter."""

from typing import Any, Callable, Optional, Awaitable
from orchestrator.domain.models import DomainEvent
from orchestrator.domain.status import ExecutionStatus
from orchestrator.runtime.interface import (
    AgentRuntime,
    ExecutionContext,
    ExecutionHandle,
    ExecutionOutcome,
    Checkpoint,
)
from orchestrator.runtime.adapters.subprocess import SubprocessAgentRuntime


class ClaudeCodeAgentRuntime(AgentRuntime):
    """Executes Claude Code CLI or Agent SDK inside configured sandboxes."""

    def __init__(self, claude_binary: str = "claude"):
        self.claude_binary = claude_binary
        self._delegate = SubprocessAgentRuntime()

    async def start(
        self,
        context: ExecutionContext,
        event_sink: Optional[Callable[[DomainEvent], Awaitable[None]]] = None,
    ) -> ExecutionHandle:
        prompt = context.inputs.get("prompt", "Execute assigned task")
        cmd = [self.claude_binary, "--print", prompt]
        adjusted_context = ExecutionContext(
            work_id=context.work_id,
            execution_id=context.execution_id,
            fencing_token=context.fencing_token,
            attempt=context.attempt,
            workspace_path=context.workspace_path,
            inputs={"command": cmd},
            env_vars=context.env_vars,
            timeout_seconds=context.timeout_seconds,
        )
        return await self._delegate.start(adjusted_context, event_sink)

    async def send_message(self, handle: ExecutionHandle, message: str) -> None:
        await self._delegate.send_message(handle, message)

    async def pause(self, handle: ExecutionHandle) -> Checkpoint:
        return await self._delegate.pause(handle)

    async def resume(self, handle: ExecutionHandle, checkpoint: Checkpoint) -> None:
        await self._delegate.resume(handle, checkpoint)

    async def cancel(self, handle: ExecutionHandle, grace_period_seconds: float = 15.0) -> None:
        await self._delegate.cancel(handle, grace_period_seconds)

    async def wait(self, handle: ExecutionHandle) -> ExecutionOutcome:
        return await self._delegate.wait(handle)
