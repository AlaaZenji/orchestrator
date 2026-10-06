"""CloudEvents 1.0 specification encoder and validator."""

import json
from datetime import datetime, timezone
from typing import Any, Dict
from orchestrator.domain.models import DomainEvent


class CloudEventFormatter:
    """Encodes and parses CNCF CloudEvents 1.0 JSON envelopes."""

    @staticmethod
    def to_json(event: DomainEvent) -> str:
        """Serializes a DomainEvent into a CloudEvents 1.0 compliant JSON string."""
        return json.dumps(event.to_cloudevent_dict(), indent=2)

    @staticmethod
    def from_dict(payload: Dict[str, Any]) -> DomainEvent:
        """Parses a CloudEvents 1.0 dictionary into a DomainEvent."""
        specversion = payload.get("specversion", "1.0")
        if specversion != "1.0":
            raise ValueError(f"Unsupported CloudEvents specversion: {specversion}")

        time_str = payload.get("time")
        dt = datetime.fromisoformat(time_str).astimezone(timezone.utc) if time_str else datetime.now(timezone.utc)

        return DomainEvent(
            id=payload["id"],
            source=payload["source"],
            type=payload["type"],
            specversion=specversion,
            time=dt,
            datacontenttype=payload.get("datacontenttype", "application/json"),
            correlation_id=payload.get("correlation_id"),
            causation_id=payload.get("causation_id"),
            sequence_number=payload.get("sequence_number", 0),
            data=payload.get("data", {}),
        )
