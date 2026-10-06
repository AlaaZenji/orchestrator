"""Local filesystem ephemeral sandbox provider with strict path isolation."""

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional
from orchestrator.sandbox.interface import SandboxProvider, CommandResult, PathTraversalError


class LocalSandboxProvider(SandboxProvider):
    """Provisions isolated directories on the local filesystem."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(tempfile.gettempdir()) / "orchestrator_sandboxes"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def create_workspace(self, work_id: str, execution_id: str) -> Path:
        safe_work = "".join(c for c in work_id if c.isalnum() or c in "-_")
        safe_exec = "".join(c for c in execution_id if c.isalnum() or c in "-_")
        workspace = self.base_dir / safe_work / safe_exec
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    async def execute_command(
        self,
        workspace_path: Path,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 60.0,
    ) -> CommandResult:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        start_time = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace_path),
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return CommandResult(
                exit_code=proc.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                duration_ms=duration_ms,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return CommandResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout_seconds}s",
                duration_ms=timeout_seconds * 1000.0,
            )

    async def destroy_workspace(self, workspace_path: Path) -> None:
        if workspace_path.exists() and workspace_path.is_relative_to(self.base_dir.resolve()):
            shutil.rmtree(workspace_path, ignore_errors=True)
