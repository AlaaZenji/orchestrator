"""Model Context Protocol (MCP) JSON-RPC tool host and router."""

from typing import Any, Callable, Dict, Optional, Awaitable
from orchestrator.tools.interface import ToolDefinition, ToolResult, ToolHandler
from orchestrator.domain.exceptions import OrchestratorError


class MCPToolHost:
    """Hosts tools and dispatches JSON-RPC 2.0 MCP protocol requests."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, ToolHandler] = {}

    def register_tool(self, tool_def: ToolDefinition, handler: ToolHandler) -> None:
        """Registers a tool with its schema definition and async execution handler."""
        self._tools[tool_def.name] = tool_def
        self._handlers[tool_def.name] = handler

    async def handle_jsonrpc(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Processes an incoming JSON-RPC 2.0 request dictionary."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema,
                        }
                        for t in self._tools.values()
                    ]
                },
            }

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if tool_name not in self._tools:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method / tool '{tool_name}' not found",
                    },
                }

            handler = self._handlers[tool_name]
            try:
                res = await handler(arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": str(res.content)}],
                        "isError": res.is_error,
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool error: {str(e)}"}],
                        "isError": True,
                    },
                }

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method '{method}' not supported by MCP host",
                },
            }
