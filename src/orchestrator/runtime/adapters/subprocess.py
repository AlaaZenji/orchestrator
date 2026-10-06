"""Subprocess runtime adapter executing agent scripts or binaries in local workspaces."""

import asyncio
import os
import signal
from typing import Any, Callable, Dict, Optional, Awaitable
from orchestrator.domain.models import DomainEvent
from orchestrator.domain.status import ExecutionStatus
from orchestrator.runtime.interface import (
    AgentRuntime,
    ExecutionContext,
    ExecutionHandle,
    ExecutionOutcome,
    Checkpoint,
)


class SubprocessAgentRuntime(AgentRuntime):
    """Executes agent workloads as child processes with streaming I/O."""

    def __init__(self, default_executable: str = "bash"):
        self.default_executable = default_executable
        self._processes: Dict[str, asyncio.subprocess.Process] = {}

    async def start(
        self,
        context: ExecutionContext,
        event_sink: Optional[Callable[[DomainEvent], Awaitable[None]]] = None,
    ) -> ExecutionHandle:
        command = context.inputs.get("command", ["echo", "Agent execution completed"])
        if isinstance(command, str):
            command = ["sh", "-c", command]

        env = os.environ.copy()
        env.update(context.env_vars)
        env["ORCHESTRATOR_WORK_ID"] = context.work_id
        env["ORCHESTRATOR_EXECUTION_ID"] = context.execution_id
        env["ORCHESTRATOR_FENCING_TOKEN"] = str(context.fencing_token)

        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(context.workspace_path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[context.execution_id] = proc

        if event_sink:
            await event_sink(
                DomainEvent(
                    type="io.orchestrator.execution.started",
                    work_id=context.work_id,
                    execution_id=context.execution_id,
                    data={"pid": proc.pid, "attempt": context.attempt},
                )
            )

        return ExecutionHandle(
            execution_id=context.execution_id,
            runtime_type="subprocess",
            process_id=proc.pid,
            native_handle=proc,
        )

    async def send_message(self, handle: ExecutionHandle, message: str) -> None:
        proc = self._processes.get(handle.execution_id)
        if proc and proc.stdin:
            proc.stdin.write(message.encode("utf-8") + b"\n")
            await proc.stdin.drain()

    async def pause(self, handle: ExecutionHandle) -> Checkpoint:
        proc = self._processes.get(handle.execution_id)
        if proc:
            proc.send_signal(signal.SIGSTOP)
        return Checkpoint(
            execution_id=handle.execution_id,
            step_index=0,
            state_payload={"paused_pid": proc.pid if proc else None},
        )

    async def resume(self, handle: ExecutionHandle, checkpoint: Checkpoint) -> None:
        proc = self._processes.get(handle.execution_id)
        if proc:
            proc.send_signal(signal.SIGCONT)

    async def cancel(self, handle: ExecutionHandle, grace_period_seconds: float = 5.0) -> None:
        proc = self._processes.get(handle.execution_id)
        if proc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_period_seconds)
            except asyncio.TimeoutError:
                proc.kill()

    async def wait(self, handle: ExecutionHandle) -> ExecutionOutcome:
        proc = self._processes.get(handle.execution_id)
        if not proc:
            return ExecutionOutcome(
                execution_id=handle.execution_id,
                status=ExecutionStatus.FAILED,
                exit_code=-1,
                error_message="Process not found",
            )

        stdout, stderr = await proc.communicate()
        status = ExecutionStatus.COMPLETED if proc.returncode == 0 else ExecutionStatus.FAILED
        return ExecutionOutcome(
            execution_id=handle.execution_id,
            status=status,
            exit_code=proc.returncode or 0,
            error_message=stderr.decode("utf-8", errors="replace") if proc.returncode != 0 else None,
            metadata={"stdout": stdout.decode("utf-8", errors="replace")[:10000]},
        )
