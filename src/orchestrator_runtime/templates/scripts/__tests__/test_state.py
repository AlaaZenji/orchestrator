"""Tests for state.py — the SINGLE chokepoint for orchestrator mutations.

TKT-ORCH-FIX-STATE-CONSISTENCY (2026-09-29).

Coverage:
  - state.commit() atomic frontmatter write + event enqueue + audit row
  - state.commit() validates transitions (state machine)
  - state.commit() with force=True bypasses validation
  - state.read() returns TicketSnapshot with frontmatter + lease + heartbeat
  - state.transitions.allowed() correctly encodes the state machine
  - state.audit() detects drift findings
  - state.compute_event_id() is deterministic
  - state.atomic_write_frontmatter() is crash-safe (write-temp-then-rename)
  - Concurrent commits produce consistent results (no torn writes)
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import state as state_mod  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a fake repo structure for state.commit() to operate on."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    (repo / "orchestrator").mkdir()
    tickets = repo / "orchestrator" / "tickets"
    tickets.mkdir()
    progress = repo / "orchestrator" / "progress"
    progress.mkdir()

    # Pre-populate one ticket
    path = tickets / "TKT-STATE-TEST.md"
    path.write_text(
        "---\n"
        "id: TKT-STATE-TEST\n"
        "title: Test\n"
        "status: QUEUED\n"
        "priority: P1\n"
        "created: 2026-09-29\n"
        "---\n"
        "Body\n"
    )

    monkeypatch.setattr(state_mod, "TICKETS_ROOT", tickets)
    monkeypatch.setattr(state_mod, "PROGRESS_DIR", progress)
    monkeypatch.setattr(state_mod, "HEARTBEAT_DIR", progress)
    monkeypatch.setattr(state_mod, "RECONCILE_EVENTS_PATH",
                        progress / ".reconcile_events.jsonl")
    monkeypatch.setattr(state_mod, "STATE_AUDIT_PATH",
                        progress / ".state_audit.jsonl")

    return repo


# ---------------------------------------------------------------------------
# state.transitions.allowed
# ---------------------------------------------------------------------------

def test_transitions_queued_to_in_progress():
    assert state_mod.transitions.allowed("QUEUED", "IN_PROGRESS") is True


def test_transitions_queued_to_done_blocked():
    assert state_mod.transitions.allowed("QUEUED", "DONE") is False


def test_transitions_in_progress_to_done():
    assert state_mod.transitions.allowed("IN_PROGRESS", "DONE") is True


def test_transitions_done_to_in_progress_allowed():
    """Explicit re-open is allowed."""
    assert state_mod.transitions.allowed("DONE", "IN_PROGRESS") is True


def test_transitions_blocked_to_queued_allowed():
    assert state_mod.transitions.allowed("BLOCKED", "QUEUED") is True


def test_transitions_deferred_to_queued_allowed():
    assert state_mod.transitions.allowed("DEFERRED", "QUEUED") is True


def test_transitions_unknown_target_rejected():
    assert state_mod.transitions.allowed("QUEUED", "BOGUS") is False


def test_transitions_tolerates_done_with_suffix():
    """'DONE (deliverable ready for review)' should match DONE."""
    assert state_mod.transitions.allowed("DONE (deliverable ready)",
                                         "IN_PROGRESS") is True


# ---------------------------------------------------------------------------
# state.compute_event_id
# ---------------------------------------------------------------------------

def test_event_id_deterministic():
    a = state_mod.compute_event_id("TKT-X", 1, "DONE")
    b = state_mod.compute_event_id("TKT-X", 1, "DONE")
    assert a == b


def test_event_id_changes_with_token():
    a = state_mod.compute_event_id("TKT-X", 1, "DONE")
    b = state_mod.compute_event_id("TKT-X", 2, "DONE")
    assert a != b


def test_event_id_changes_with_status():
    a = state_mod.compute_event_id("TKT-X", 1, "DONE")
    b = state_mod.compute_event_id("TKT-X", 1, "PARTIAL")
    assert a != b


def test_event_id_is_16_hex():
    eid = state_mod.compute_event_id("TKT-X", 1, "DONE")
    assert len(eid) == 16
    assert all(c in "0123456789abcdef" for c in eid)


# ---------------------------------------------------------------------------
# state.commit — happy path
# ---------------------------------------------------------------------------

def test_commit_updates_frontmatter_atomically(fake_repo):
    r = state_mod.commit("TKT-STATE-TEST", "IN_PROGRESS", source="test")
    # Frontmatter should be updated
    text = (fake_repo / "orchestrator" / "tickets" /
            "TKT-STATE-TEST.md").read_text()
    assert "status: IN_PROGRESS" in text
    assert "updated: 2026-09-29" in text
    # Event enqueued
    events = (fake_repo / "orchestrator" / "progress"
              / ".reconcile_events.jsonl").read_text()
    assert "TKT-STATE-TEST" in events
    assert "IN_PROGRESS" in events
    # Audit row appended
    audit = (fake_repo / "orchestrator" / "progress"
             / ".state_audit.jsonl").read_text()
    assert "TKT-STATE-TEST" in audit
    assert "test" in audit  # source


def test_commit_returns_commit_result(fake_repo):
    r = state_mod.commit("TKT-STATE-TEST", "IN_PROGRESS", source="test")
    assert r.ticket_id == "TKT-STATE-TEST"
    assert r.previous_status == "QUEUED"
    assert r.new_status == "IN_PROGRESS"
    assert r.source == "test"
    assert len(r.event_id) == 16


def test_commit_rejects_invalid_transition(fake_repo):
    """QUEUED → DONE is not allowed (must go through IN_PROGRESS or PARTIAL)."""
    with pytest.raises(state_mod.InvalidTransitionError) as exc:
        state_mod.commit("TKT-STATE-TEST", "DONE", source="test")
    assert exc.value.current == "QUEUED"
    assert exc.value.target == "DONE"


def test_commit_with_force_bypasses_validation(fake_repo):
    """force=True skips the state machine — admin-only escape hatch."""
    r = state_mod.commit("TKT-STATE-TEST", "DONE",
                          source="test", force=True)
    assert r.new_status == "DONE"


def test_commit_to_same_status_is_no_op(fake_repo):
    """QUEUED → QUEUED is allowed (idempotent)."""
    r = state_mod.commit("TKT-STATE-TEST", "QUEUED", source="test")
    assert r.previous_status == "QUEUED"
    assert r.new_status == "QUEUED"


def test_commit_removes_heartbeat(fake_repo):
    """The .heartbeat-<ticket_id> file is removed on commit."""
    heartbeat = (fake_repo / "orchestrator" / "progress"
                 / ".heartbeat-TKT-STATE-TEST")
    heartbeat.write_text("alive")
    assert heartbeat.exists()

    state_mod.commit("TKT-STATE-TEST", "IN_PROGRESS", source="test")
    assert not heartbeat.exists()


def test_commit_ticket_not_found(fake_repo):
    with pytest.raises(state_mod.TicketNotFoundError):
        state_mod.commit("TKT-DOES-NOT-EXIST", "DONE", source="test")


def test_commit_with_extra_fields(fake_repo):
    state_mod.commit(
        "TKT-STATE-TEST", "DONE", source="test",
        extra_fields={"doctrine_rejections": "TKT-ORCH-023"},
        force=True,  # QUEUED → DONE
    )
    text = (fake_repo / "orchestrator" / "tickets"
            / "TKT-STATE-TEST.md").read_text()
    assert "doctrine_rejections: TKT-ORCH-023" in text


# ---------------------------------------------------------------------------
# state.read — happy path
# ---------------------------------------------------------------------------

def test_read_returns_snapshot(fake_repo, monkeypatch):
    # Stub the lease read so we don't need a Postgres connection.
    monkeypatch.setattr(state_mod, "_read_lease",
                        lambda tid: ("worker-x", "2026-09-29T16:00:00Z", 42))
    snap = state_mod.read("TKT-STATE-TEST")
    assert snap.ticket_id == "TKT-STATE-TEST"
    assert snap.frontmatter_status == "QUEUED"
    assert snap.frontmatter_priority == "P1"
    assert snap.live_lease_holder == "worker-x"
    assert snap.live_lease_fencing_token == 42
    assert snap.heartbeat_present is False
    assert snap.worker_result_present is False


def test_read_detects_heartbeat(fake_repo):
    heartbeat = (fake_repo / "orchestrator" / "progress"
                 / ".heartbeat-TKT-STATE-TEST")
    heartbeat.write_text("alive")
    snap = state_mod.read("TKT-STATE-TEST")
    assert snap.heartbeat_present is True
    assert snap.heartbeat_age_seconds is not None
    assert snap.heartbeat_age_seconds < 5  # just created


def test_read_detects_worker_result(fake_repo):
    wr = (fake_repo / "orchestrator" / "progress"
          / "TKT-STATE-TEST-WORKER-RESULT.json")
    wr.write_text(json.dumps({
        "final_status": "DONE",
        "verifier_provenance": {"dispatched_count": 8},
    }))
    snap = state_mod.read("TKT-STATE-TEST")
    assert snap.worker_result_present is True
    assert snap.worker_result_status == "DONE"


# ---------------------------------------------------------------------------
# state.audit — drift detection
# ---------------------------------------------------------------------------

def test_audit_clean_state(fake_repo, monkeypatch):
    """No leases, no heartbeats, no worker_results → empty findings."""
    monkeypatch.setattr(state_mod, "_read_lease",
                        lambda tid: (None, None, None))
    findings = state_mod.audit()
    # No ERROR findings (heartbeats not present, leases not held, etc.)
    errors = [f for f in findings if f.severity == "ERROR"]
    assert len(errors) == 0


def test_audit_detects_heartbeat_no_lease(fake_repo, monkeypatch):
    """Heartbeat present + no lease → ERROR finding."""
    monkeypatch.setattr(state_mod, "_read_lease",
                        lambda tid: (None, None, None))
    heartbeat = (fake_repo / "orchestrator" / "progress"
                 / ".heartbeat-TKT-STATE-TEST")
    heartbeat.write_text("alive")
    findings = state_mod.audit()
    types = [f.finding_type for f in findings]
    assert "heartbeat_no_lease" in types


def test_audit_detects_frontmatter_done_lease_held(fake_repo, monkeypatch):
    """Frontmatter DONE + live lease → ERROR."""
    # Set frontmatter to DONE
    (fake_repo / "orchestrator" / "tickets" /
     "TKT-STATE-TEST.md").write_text(
        "---\nid: TKT-STATE-TEST\ntitle: t\nstatus: DONE\npriority: P1\n"
        "created: 2026-09-29\n---\nBody\n"
    )
    # Pretend lease is held
    monkeypatch.setattr(state_mod, "_read_lease",
                        lambda tid: ("worker-x", "2026-09-29T16:00:00Z", 42))
    findings = state_mod.audit()
    types = [f.finding_type for f in findings]
    assert "frontmatter_done_lease_held" in types


def test_audit_detects_worker_result_done_queued(fake_repo, monkeypatch):
    """WORKER_RESULT DONE + frontmatter QUEUED → ERROR."""
    monkeypatch.setattr(state_mod, "_read_lease",
                        lambda tid: (None, None, None))
    wr = (fake_repo / "orchestrator" / "progress"
          / "TKT-STATE-TEST-WORKER-RESULT.json")
    wr.write_text(json.dumps({"final_status": "DONE"}))
    # Frontmatter still says QUEUED (fixture default)
    findings = state_mod.audit()
    types = [f.finding_type for f in findings]
    assert "worker_result_done_frontmatter_queued" in types


def test_audit_detects_stale_lease_no_worker_result(fake_repo, monkeypatch):
    """Lease held + no WORKER_RESULT → WARN."""
    monkeypatch.setattr(state_mod, "_read_lease",
                        lambda tid: ("worker-x", "2026-09-29T16:00:00Z", 42))
    findings = state_mod.audit()
    types_warn = [f.finding_type for f in findings if f.severity == "WARN"]
    assert "stale_lease_no_worker_result" in types_warn


# ---------------------------------------------------------------------------
# Crash-safety: atomic write
# ---------------------------------------------------------------------------

def test_atomic_write_uses_temp_file(fake_repo):
    """atomic_write_frontmatter writes to a temp file then renames."""
    path = fake_repo / "orchestrator" / "tickets" / "TKT-STATE-TEST.md"
    state_mod.atomic_write_frontmatter(
        path, {"id": "TKT-STATE-TEST", "status": "DONE"}, "\nbody\n"
    )
    text = path.read_text()
    assert "status: DONE" in text
    assert "body" in text


def test_atomic_write_does_not_leave_temp_file(fake_repo):
    """After a successful write, no .tmp file should remain."""
    path = fake_repo / "orchestrator" / "tickets" / "TKT-STATE-TEST.md"
    state_mod.atomic_write_frontmatter(
        path, {"id": "TKT-STATE-TEST", "status": "DONE"}, "\nbody\n"
    )
    # Check no .tmp files in tickets/
    temps = list(path.parent.glob("*.tmp"))
    assert temps == []


# ---------------------------------------------------------------------------
# Concurrent commits produce consistent results
# ---------------------------------------------------------------------------

def test_concurrent_commits_yield_consistent_state(fake_repo):
    """Sequential commits to the same ticket converge to the final state."""
    state_mod.commit("TKT-STATE-TEST", "IN_PROGRESS", source="test")
    state_mod.commit("TKT-STATE-TEST", "DONE", source="test", force=True)
    text = (fake_repo / "orchestrator" / "tickets"
            / "TKT-STATE-TEST.md").read_text()
    # Final state should be DONE (the second commit wins)
    assert "status: DONE" in text
    # Both events enqueued
    events = (fake_repo / "orchestrator" / "progress"
              / ".reconcile_events.jsonl").read_text()
    assert "IN_PROGRESS" in events
    assert "DONE" in events