"""Agent-to-Agent (A2A) protocol models and task translation adapter."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from orchestrator.domain.models import Work
from orchestrator.domain.status import WorkStatus


class A2ATaskStatus(str, Enum):
    """Standard A2A Task states defined by Linux Foundation."""
    SUBMITTED = "SUBMITTED"
    WORKING = "WORKING"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class AgentCard:
    """A2A machine-readable Agent Card describing capabilities."""
    name: str
    description: str
    version: str
    capabilities: List[str]
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    author: str = "Orchestrator Platform"


class A2AAdapter:
    """Translates between A2A tasks and Orchestrator Work entities."""

    WORK_STATUS_TO_A2A = {
        WorkStatus.PENDING: A2ATaskStatus.SUBMITTED,
        WorkStatus.ELIGIBLE: A2ATaskStatus.SUBMITTED,
        WorkStatus.RUNNING: A2ATaskStatus.WORKING,
        WorkStatus.WAITING_VERIFICATION: A2ATaskStatus.WORKING,
        WorkStatus.AWAITING_APPROVAL: A2ATaskStatus.INPUT_REQUIRED,
        WorkStatus.RETRYING: A2ATaskStatus.WORKING,
        WorkStatus.SUCCEEDED: A2ATaskStatus.COMPLETED,
        WorkStatus.FAILED: A2ATaskStatus.FAILED,
        WorkStatus.CANCELLED: A2ATaskStatus.CANCELLED,
    }

    @classmethod
    def work_to_a2a_status(cls, status: WorkStatus) -> A2ATaskStatus:
        return cls.WORK_STATUS_TO_A2A.get(status, A2ATaskStatus.WORKING)

    @classmethod
    def work_to_a2a_task_dict(cls, work: Work) -> Dict[str, Any]:
        return {
            "taskId": work.work_id,
            "status": cls.work_to_a2a_status(work.status).value,
            "title": work.title,
            "description": work.description,
            "createdAt": work.created_at.isoformat(),
            "updatedAt": work.updated_at.isoformat(),
            "metadata": {
                "priority": work.priority,
                "retryCount": work.retry_count,
                "fencingToken": work.active_fencing_token,
            },
        }
