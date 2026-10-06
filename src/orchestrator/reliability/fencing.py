"""Fenced gateway and resource-side fencing validation."""

from typing import Optional, Callable
from orchestrator.domain.exceptions import StaleFencingTokenError


class FencedGateway:
    """Verifies fencing tokens at the storage or resource barrier before applying side effects."""

    @staticmethod
    def verify_token(work_id: str, presented_token: int, active_watermark: int) -> None:
        """Enforces that the presented fencing token matches or exceeds the active watermark."""
        if presented_token < active_watermark:
            raise StaleFencingTokenError(
                work_id=work_id,
                presented_token=presented_token,
                active_token=active_watermark,
            )

    @classmethod
    def execute_fenced(
        cls,
        work_id: str,
        presented_token: int,
        get_active_token_fn: Callable[[], int],
        action_fn: Callable[[], None],
    ) -> None:
        """Executes action_fn within a fenced barrier."""
        active_token = get_active_token_fn()
        cls.verify_token(work_id, presented_token, active_token)
        action_fn()
