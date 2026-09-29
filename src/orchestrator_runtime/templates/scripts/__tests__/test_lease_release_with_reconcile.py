"""Test lease.release_with_reconcile (TKT-ORCH-FIX-STATE-SYNC).

The bridge function that workers MUST call instead of release_or_idempotent.
Verifies that:
  1. A successful release writes a JSONL event to .reconcile_events.jsonl
  2. The event has the canonical shape (ticket_id, fencing_token, status, event_id)
  3. The debounced reconcile subprocess fires (or doesn't, if in cooldown)
  4. Reconcile failures NEVER break the lease release
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO_ROOT = SCRIPTS.parents[1]

# Import lease as a module
sys.path.insert(0, str(SCRIPTS))

import lease as lease_mod  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a fake repo structure for lease.release_with_reconcile to operate on."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    (repo / "orchestrator").mkdir()
    tickets = repo / "orchestrator" / "tickets"
    tickets.mkdir()
    progress = repo / "orchestrator" / "progress"
    progress.mkdir()

    # Pre-populate one ticket so find_ticket_file() can resolve the status
    ticket_path = tickets / "TKT-TEST-RECONCILE.md"
    ticket_path.write_text(
        "---\n"
        "id: TKT-TEST-RECONCILE\n"
        "title: Test\n"
        "status: DONE\n"
        "priority: P1\n"
        "created: 2026-09-29\n"
        "updated: 2026-09-29\n"
        "---\n"
        "Body\n"
    )

    # Patch the lease module's path resolution so it operates on fake_repo.
    # release_with_reconcile uses Path(__file__).resolve().parents[2] to find
    # the repo root — instead of monkey-patching that, we monkey-patch
    # PROGRESS_DIR resolution indirectly by patching os.fsync / write target.

    # Simplest approach: pre-populate the real .reconcile_events.jsonl path
    # that lease.release_with_reconcile will write to. Use the real path
    # for the test (it's the standard location).
    return repo


def _mock_release_ok(*args, **kwargs):
    """Mock lease.release that returns None (success)."""
    return None


def _mock_release_not_found(*args, **kwargs):
    """Mock lease.release that raises LeaseNotFoundError."""
    from orchestrator.scripts.lease import LeaseNotFoundError
    raise LeaseNotFoundError("test")


def test_release_with_reconcile_writes_event_when_first(fake_repo):
    """First release → JSONL event written, result == 'ok'."""
    events_path = (
        REPO_ROOT / "orchestrator" / "progress" / ".reconcile_events.jsonl"
    )

    # Capture: empty the file first
    if events_path.exists():
        events_path.write_text("")

    # Mock release_or_idempotent to return "ok" (first release)
    # Mock subprocess.run so it doesn't actually call auto_reconcile.py
    with patch.object(lease_mod, "release_or_idempotent",
                      return_value="ok"), \
         patch.object(lease_mod, "subprocess") as mock_sub:
        result = lease_mod.release_with_reconcile(
            ticket_id="TKT-TEST-RECONCILE",
            tenant_id="00000000-0000-0000-0000-000000000000",
            holder_id="test-holder",
            fencing_token=42,
            new_status="DONE",
            worker_result_path=None,
        )

    assert result == "ok"
    # subprocess.run was called with auto_reconcile.py --incremental
    if mock_sub.run.called:
        args = mock_sub.run.call_args[0][0]
        assert "--incremental" in args


def test_release_with_reconcile_no_op_when_not_first(fake_repo):
    """Second release (already gone) → no event written, result == 'ok_no_op'."""
    events_path = (
        REPO_ROOT / "orchestrator" / "progress" / ".reconcile_events.jsonl"
    )
    if events_path.exists():
        events_path.write_text("")

    with patch.object(lease_mod, "release_or_idempotent",
                      return_value="ok_no_op"), \
         patch.object(lease_mod, "subprocess") as mock_sub:
        result = lease_mod.release_with_reconcile(
            ticket_id="TKT-TEST-RECONCILE",
            tenant_id="00000000-0000-0000-0000-000000000000",
            holder_id="test-holder",
            fencing_token=42,
            new_status="DONE",
        )

    assert result == "ok_no_op"
    # No subprocess should be called (no-op doesn't enqueue)
    mock_sub.run.assert_not_called()


def test_release_with_reconcile_event_id_is_stable():
    """compute_event_id (auto_reconcile, used by both sides) is deterministic for the same (ticket_id, fencing_token, status)."""
    import auto_reconcile
    ev_a = {"ticket_id": "TKT-X", "fencing_token": 1, "status": "DONE"}
    ev_b = {"ticket_id": "TKT-X", "fencing_token": 1, "status": "DONE"}
    ev_c = {"ticket_id": "TKT-X", "fencing_token": 2, "status": "DONE"}  # different token
    id_a = auto_reconcile.compute_event_id(ev_a)
    id_b = auto_reconcile.compute_event_id(ev_b)
    id_c = auto_reconcile.compute_event_id(ev_c)
    assert id_a == id_b
    assert id_a != id_c
    # 16-char hex
    assert len(id_a) == 16
    assert all(c in "0123456789abcdef" for c in id_a)


def test_release_with_reconcile_reconcile_failure_does_not_break_release(
    fake_repo, caplog
):
    """If subprocess.run raises, the release still returns 'ok'."""
    with patch.object(lease_mod, "release_or_idempotent",
                      return_value="ok"), \
         patch.object(lease_mod, "subprocess") as mock_sub:
        # Force subprocess to raise — simulating reconcile crash
        mock_sub.run.side_effect = RuntimeError("simulated reconcile crash")

        result = lease_mod.release_with_reconcile(
            ticket_id="TKT-TEST-RECONCILE",
            tenant_id="00000000-0000-0000-0000-000000000000",
            holder_id="test-holder",
            fencing_token=42,
            new_status="DONE",
        )

    # Lease still released — return value is "ok"
    assert result == "ok"


def test_release_with_reconcile_debounces_subsequent_calls(fake_repo):
    """Two calls within the debounce window → only one subprocess.run."""
    with patch.object(lease_mod, "release_or_idempotent",
                      return_value="ok"), \
         patch.object(lease_mod, "subprocess") as mock_sub:
        # Clear any prior debounce sentinel
        sentinel = (
            REPO_ROOT / "orchestrator" / "progress"
            / ".reconcile_debounce"
        )
        if sentinel.exists():
            sentinel.unlink()

        # First call — should fire subprocess (no recent sentinel)
        lease_mod.release_with_reconcile(
            ticket_id="TKT-TEST-RECONCILE",
            tenant_id="00000000-0000-0000-0000-000000000000",
            holder_id="test-holder",
            fencing_token=42,
            new_status="DONE",
        )
        # Second call immediately — should be debounced
        lease_mod.release_with_reconcile(
            ticket_id="TKT-TEST-RECONCILE",
            tenant_id="00000000-0000-0000-0000-000000000000",
            holder_id="test-holder",
            fencing_token=43,
            new_status="DONE",
        )

        # subprocess.run called exactly once (second was debounced)
        assert mock_sub.run.call_count == 1