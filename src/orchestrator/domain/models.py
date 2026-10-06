"""Domain models for durable agent orchestration."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional
import uuid

from orchestrator.domain.status import WorkStatus, ExecutionStatus, ActorRole, VerifierVerdict


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ActorContext:
    """Identity and authorization attributes of the caller."""
    actor_id: str
    role: ActorRole
    tenant_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Work:
    """The central stateful Work entity."""
    work_id: str
    title: str
    description: str = ""
    status: WorkStatus = WorkStatus.PENDING
    priority: int = 0
    max_retries: int = 3
    retry_count: int = 0
    timeout_seconds: int = 3600
    heartbeat_timeout_seconds: int = 180
    dependencies: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    active_fencing_token: Optional[int] = None
    active_execution_id: Optional[str] = None
    tenant_id: str = "default"
    version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


@dataclass(frozen=True)
class Execution:
    """An individual execution attempt of a Work unit."""
    execution_id: str
    work_id: str
    attempt: int
    fencing_token: int
    status: ExecutionStatus = ExecutionStatus.STARTING
    worker_id: str = ""
    runtime_type: str = "subprocess"
    workspace_path: Optional[str] = None
    exit_code: Optional[int] = None
    error_message: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utc_now)
    ended_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["started_at"] = self.started_at.isoformat()
        d["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        return d


@dataclass(frozen=True)
class Lease:
    """Exclusive, time-bounded ownership lease over a Work unit."""
    work_id: str
    holder_id: str
    fencing_token: int
    expires_at: datetime
    acquired_at: datetime = field(default_factory=utc_now)
    tenant_id: str = "default"

    def is_expired(self, current_time: Optional[datetime] = None) -> bool:
        now = current_time or utc_now()
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "holder_id": self.holder_id,
            "fencing_token": self.fencing_token,
            "expires_at": self.expires_at.isoformat(),
            "acquired_at": self.acquired_at.isoformat(),
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True)
class Artifact:
    """An immutable, content-addressed output artifact."""
    artifact_id: str
    execution_id: str
    name: str
    digest_sha256: str
    storage_uri: str
    size_bytes: int
    mime_type: str = "application/octet-stream"
    is_promoted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass(frozen=True)
class VerificationResult:
    """Record of an independent verification check."""
    verification_id: str
    execution_id: str
    verifier_name: str
    verdict: VerifierVerdict
    passed: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    executed_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["executed_at"] = self.executed_at.isoformat()
        return d


@dataclass(frozen=True)
class ApprovalRequest:
    """A human-in-the-loop approval requirement."""
    request_id: str
    work_id: str
    execution_id: str
    action_type: str
    description: str
    requested_by: str
    is_approved: Optional[bool] = None
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["decided_at"] = self.decided_at.isoformat() if self.decided_at else None
        return d


@dataclass(frozen=True)
class DomainEvent:
    """CNCF CloudEvents 1.0 compliant domain event."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "/orchestrator"
    type: str = "io.orchestrator.generic"
    specversion: str = "1.0"
    time: datetime = field(default_factory=utc_now)
    datacontenttype: str = "application/json"
    data: dict[str, Any] = field(default_factory=dict)
    work_id: Optional[str] = None
    execution_id: Optional[str] = None
    sequence_number: int = 0
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None

    def to_cloudevent_dict(self) -> dict[str, Any]:
        return {
            "specversion": self.specversion,
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "time": self.time.isoformat(),
            "datacontenttype": self.datacontenttype,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "sequence_number": self.sequence_number,
            "data": self.data,
        }
