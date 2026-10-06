"""Tool definitions and invocation interfaces."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Awaitable


@dataclass(frozen=True)
class ToolDefinition:
    """Specification of an agent-executable tool matching JSON Schema."""
    name: str
    description: str
    input_schema: Dict[str, Any]
    is_mutation: bool = False
    requires_approval: bool = False


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a tool execution."""
    content: Any
    is_error: bool = False
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[Dict[str, Any]], Awaitable[ToolResult]]
