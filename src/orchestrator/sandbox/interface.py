"""Sandbox provider interface and path safety primitives."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from orchestrator.domain.exceptions import OrchestratorError


class PathTraversalError(OrchestratorError):
    """Raised when an agent attempts to access a path outside its designated workspace."""
    def __init__(self, requested_path: str, workspace_root: Path):
        super().__init__(
            f"Path traversal detected: '{requested_path}' escapes workspace root '{workspace_root}'.",
            code="PATH_TRAVERSAL_DETECTED",
            status_code=403,
        )


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a command executed inside a sandbox."""
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float = 0.0


class SandboxProvider(ABC):
    """Abstract interface for managing quarantined agent execution environments."""

    @abstractmethod
    async def create_workspace(self, work_id: str, execution_id: str) -> Path:
        """Provisions an isolated, ephemeral workspace directory."""
        ...

    @abstractmethod
    async def execute_command(
        self,
        workspace_path: Path,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 60.0,
    ) -> CommandResult:
        """Executes a command within the quarantined sandbox boundary."""
        ...

    @abstractmethod
    async def destroy_workspace(self, workspace_path: Path) -> None:
        """Securely deletes the ephemeral workspace and cleans up mounts."""
        ...

    @staticmethod
    def resolve_safe_path(workspace_root: Path, relative_path: str) -> Path:
        """Validates that relative_path does not escape workspace_root.
        Raises PathTraversalError if traversal is detected.
        """
        resolved_root = workspace_root.resolve()
        target = (resolved_root / relative_path).resolve()
        if not target.is_relative_to(resolved_root):
            raise PathTraversalError(relative_path, resolved_root)
        return target
