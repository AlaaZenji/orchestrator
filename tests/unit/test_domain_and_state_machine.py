"""Unit tests for domain models, state machines, and reliability primitives."""

import pytest
from datetime import timedelta
from pathlib import Path

from orchestrator.domain import (
    Work,
    Execution,
    Lease,
    ActorContext,
    WorkStatus,
    ExecutionStatus,
    ActorRole,
    VerifierVerdict,
    VerificationResult,
    DomainEvent,
    InvalidTransitionError,
    UnauthorizedTransitionError,
    StaleFencingTokenError,
    IdempotencyConflictError,
    utc_now,
)
from orchestrator.orchestration import (
    WorkStateMachine,
    ExecutionStateMachine,
    DependencyResolver,
    CyclicDependencyError,
    Scheduler,
)
from orchestrator.reliability import (
    LeaseManager,
    FencedGateway,
    IdempotencyManager,
    LeaseWatchdog,
    CrashReconciler,
    RetryPolicy,
)
from orchestrator.sandbox import PathTraversalError, SandboxProvider
from orchestrator.tools import ToolDefinition, ToolResult, MCPToolHost
from orchestrator.events import CloudEventFormatter


def test_work_state_machine_legal_progression():
    """Verify normal legal progression from PENDING -> ELIGIBLE -> RUNNING -> WAITING_VERIFICATION -> SUCCEEDED."""
    work = Work(work_id="TKT-1", title="Test task", status=WorkStatus.PENDING)
    system = ActorContext(actor_id="sys", role=ActorRole.SYSTEM_RECONCILER)
    agent = ActorContext(actor_id="agent-1", role=ActorRole.AGENT_WORKER)
    verifier = ActorContext(actor_id="verifier", role=ActorRole.AUTHORIZED_VERIFIER)

    # PENDING -> ELIGIBLE
    WorkStateMachine.validate_transition(work, WorkStatus.ELIGIBLE, system)
    work = Work(work_id="TKT-1", title="Test task", status=WorkStatus.ELIGIBLE)

    # ELIGIBLE -> RUNNING
    WorkStateMachine.validate_transition(work, WorkStatus.RUNNING, agent)
    work = Work(work_id="TKT-1", title="Test task", status=WorkStatus.RUNNING, active_fencing_token=10)

    # RUNNING -> WAITING_VERIFICATION
    WorkStateMachine.validate_transition(work, WorkStatus.WAITING_VERIFICATION, agent, presented_fencing_token=10)
    work = Work(work_id="TKT-1", title="Test task", status=WorkStatus.WAITING_VERIFICATION)

    # WAITING_VERIFICATION -> SUCCEEDED
    WorkStateMachine.validate_transition(work, WorkStatus.SUCCEEDED, verifier)


def test_agent_cannot_self_complete_work():
    """INV-004: An agent cannot directly transition work to SUCCEEDED."""
    work = Work(work_id="TKT-1", title="Test task", status=WorkStatus.WAITING_VERIFICATION)
    agent = ActorContext(actor_id="agent-1", role=ActorRole.AGENT_WORKER)

    with pytest.raises(UnauthorizedTransitionError):
        WorkStateMachine.validate_transition(work, WorkStatus.SUCCEEDED, agent)


def test_agent_cannot_self_approve_work():
    """An agent cannot self-approve work from AWAITING_APPROVAL."""
    work = Work(work_id="TKT-1", title="Test task", status=WorkStatus.AWAITING_APPROVAL)
    agent = ActorContext(actor_id="agent-1", role=ActorRole.AGENT_WORKER)

    with pytest.raises(UnauthorizedTransitionError):
        WorkStateMachine.validate_transition(work, WorkStatus.RUNNING, agent)


def test_terminal_state_immutability():
    """Terminal states (SUCCEEDED, FAILED, CANCELLED) are strictly immutable."""
    for term_status in (WorkStatus.SUCCEEDED, WorkStatus.FAILED, WorkStatus.CANCELLED):
        work = Work(work_id="TKT-1", title="Test task", status=term_status)
        system = ActorContext(actor_id="sys", role=ActorRole.SYSTEM_RECONCILER)
        with pytest.raises(InvalidTransitionError):
            WorkStateMachine.validate_transition(work, WorkStatus.RUNNING, system)


def test_fencing_token_validation_rejects_stale():
    """INV-002: Stale fencing token presentation must be rejected."""
    work = Work(work_id="TKT-1", title="Test", status=WorkStatus.RUNNING, active_fencing_token=42)
    agent = ActorContext(actor_id="agent-1", role=ActorRole.AGENT_WORKER)

    # Token 41 presented against active token 42 -> REJECTED
    with pytest.raises(StaleFencingTokenError):
        WorkStateMachine.validate_transition(
            work, WorkStatus.WAITING_VERIFICATION, agent, presented_fencing_token=41
        )

    # Token 42 presented -> ACCEPTED
    WorkStateMachine.validate_transition(
        work, WorkStatus.WAITING_VERIFICATION, agent, presented_fencing_token=42
    )


def test_dependency_resolver_detects_cycles():
    """Dependency resolver correctly detects directed cycles."""
    w1 = Work(work_id="A", title="A", dependencies=("B",))
    w2 = Work(work_id="B", title="B", dependencies=("C",))
    w3 = Work(work_id="C", title="C", dependencies=("A",))

    with pytest.raises(CyclicDependencyError):
        DependencyResolver.detect_cycles([w1, w2, w3])


def test_dependency_resolver_ready_calculation():
    """Dependency resolver correctly computes eligible works."""
    w1 = Work(work_id="A", title="A", status=WorkStatus.SUCCEEDED)
    w2 = Work(work_id="B", title="B", status=WorkStatus.PENDING, dependencies=("A",))
    w3 = Work(work_id="C", title="C", status=WorkStatus.PENDING, dependencies=("B",))

    eligible = DependencyResolver.compute_eligible_works([w1, w2, w3])
    assert len(eligible) == 1
    assert eligible[0].work_id == "B"


def test_lease_manager_monotonic_sequencing():
    """Lease manager issues strictly increasing sequence tokens."""
    lm = LeaseManager()
    l1 = lm.create_lease("TKT-1", "worker-A", ttl_seconds=60)
    l2 = lm.create_lease("TKT-2", "worker-B", ttl_seconds=60)
    l3 = lm.create_lease("TKT-1", "worker-C", ttl_seconds=60)

    assert l1.fencing_token < l2.fencing_token < l3.fencing_token


def test_idempotency_manager():
    """Idempotency manager caches responses and detects conflicting payloads."""
    im = IdempotencyManager()
    key = "idem-123"
    payload = {"foo": "bar"}

    assert im.check_or_record(key, "test", payload) is None
    im.save_response(key, "test", payload, 200, {"result": "ok"})

    rec = im.check_or_record(key, "test", payload)
    assert rec is not None
    assert rec.response_status == 200

    # Conflicting payload
    with pytest.raises(IdempotencyConflictError):
        im.check_or_record(key, "test", {"foo": "different"})


def test_sandbox_path_traversal_detection():
    """Sandbox safely detects directory traversal attempts."""
    root = Path("/tmp/orchestrator_sandbox_test").resolve()
    # Safe path
    safe = SandboxProvider.resolve_safe_path(root, "src/main.py")
    assert safe.is_relative_to(root)

    # Malicious traversal
    with pytest.raises(PathTraversalError):
        SandboxProvider.resolve_safe_path(root, "../../etc/passwd")


@pytest.mark.anyio
async def test_mcp_tool_host():
    """MCP host correctly lists and executes tools via JSON-RPC."""
    host = MCPToolHost()
    t_def = ToolDefinition(
        name="echo_tool",
        description="Echoes input",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
    )
    async def echo_handler(args):
        return ToolResult(content=f"Echo: {args.get('msg')}")

    host.register_tool(t_def, echo_handler)

    # tools/list
    res_list = await host.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "tools" in res_list["result"]
    assert res_list["result"]["tools"][0]["name"] == "echo_tool"

    # tools/call
    res_call = await host.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "echo_tool", "arguments": {"msg": "hello world"}},
    })
    assert res_call["result"]["content"][0]["text"] == "Echo: hello world"
    assert not res_call["result"]["isError"]


def test_cloudevents_formatting():
    """CloudEventFormatter encodes and decodes CNCF CloudEvents 1.0."""
    evt = DomainEvent(
        type="io.orchestrator.work.created",
        work_id="TKT-100",
        data={"priority": 10},
    )
    raw_json = CloudEventFormatter.to_json(evt)
    assert '"specversion": "1.0"' in raw_json
    assert '"type": "io.orchestrator.work.created"' in raw_json
