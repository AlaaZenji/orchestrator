"""Idempotency key coordination and replay caching."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Dict
from orchestrator.domain.models import utc_now
from orchestrator.domain.exceptions import IdempotencyConflictError


@dataclass(frozen=True)
class IdempotencyRecord:
    """Cached execution response for an idempotent operation."""
    key: str
    action_type: str
    request_hash: str
    response_status: int
    response_body: str
    created_at: datetime = field(default_factory=utc_now)


class IdempotencyManager:
    """Manages idempotency keys and cached response replays."""

    def __init__(self):
        self._records: Dict[str, IdempotencyRecord] = {}

    @staticmethod
    def compute_hash(payload: Any) -> str:
        """Computes a deterministic SHA-256 digest of arbitrary serializable payload."""
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def check_or_record(
        self,
        idempotency_key: str,
        action_type: str,
        payload: Any,
    ) -> Optional[IdempotencyRecord]:
        """Checks if key exists.
        - If exists with identical hash: returns cached record.
        - If exists with different hash: raises IdempotencyConflictError.
        - If does not exist: returns None.
        """
        req_hash = self.compute_hash(payload)
        existing = self._records.get(idempotency_key)
        if existing:
            if existing.request_hash != req_hash:
                raise IdempotencyConflictError(idempotency_key)
            return existing
        return None

    def save_response(
        self,
        idempotency_key: str,
        action_type: str,
        payload: Any,
        status_code: int,
        response_data: Any,
    ) -> IdempotencyRecord:
        """Persists the response for future idempotent replays."""
        req_hash = self.compute_hash(payload)
        body_str = json.dumps(response_data, default=str)
        record = IdempotencyRecord(
            key=idempotency_key,
            action_type=action_type,
            request_hash=req_hash,
            response_status=status_code,
            response_body=body_str,
        )
        self._records[idempotency_key] = record
        return record
