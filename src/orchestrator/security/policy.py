"""Deterministic security policy engine evaluating tool permissions and transitions."""

from typing import Dict, List, Optional
from orchestrator.domain.models import ActorContext
from orchestrator.domain.status import ActorRole
from orchestrator.domain.exceptions import OrchestratorError


class SecurityPolicyError(OrchestratorError):
    """Raised when an action violates security policy rules."""
    def __init__(self, action: str, reason: str):
        super().__init__(
            f"Security policy denied action '{action}': {reason}",
            code="SECURITY_POLICY_VIOLATION",
            status_code=403,
        )


class SecurityPolicyEngine:
    """Evaluates access control rules and approval triggers outside the LLM."""

    def __init__(
        self,
        allowed_tools: Optional[List[str]] = None,
        disallowed_commands: Optional[List[str]] = None,
        require_approval_for_mutations: bool = False,
    ):
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        self.disallowed_commands = disallowed_commands or [
            "rm -rf /",
            "DROP DATABASE",
            "DROP SCHEMA",
            "sudo",
        ]
        self.require_approval_for_mutations = require_approval_for_mutations

    def authorize_tool_call(self, tool_name: str, arguments: Dict[str, Any], actor: ActorContext) -> bool:
        """Determines if the given actor is authorized to execute the tool."""
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            raise SecurityPolicyError(tool_name, f"Tool '{tool_name}' is not in the allowed list.")

        # Check for banned substrings in string arguments
        for k, v in arguments.items():
            if isinstance(v, str):
                for banned in self.disallowed_commands:
                    if banned.lower() in v.lower():
                        raise SecurityPolicyError(tool_name, f"Disallowed pattern '{banned}' detected in parameter '{k}'.")

        return True

    def requires_human_approval(self, action_type: str, is_mutation: bool) -> bool:
        """Determines if an action must halt for human review."""
        if self.require_approval_for_mutations and is_mutation:
            return True
        return action_type in ("schema_migration", "secret_rotation", "production_deploy")
