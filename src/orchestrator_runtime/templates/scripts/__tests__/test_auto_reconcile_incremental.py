"""Test the --incremental mode of auto_reconcile.py.

TKT-CORE-FIX-STATE-SYNC (2026-09-29): drain the reconcile-events queue,
append one cascade delta row per event to tickets-index.md, update the
at-a-glance DONE count, and truncate the events file. Idempotent:
applying the same event twice is a no-op.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Path setup: import auto_reconcile as a module
HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import auto_reconcile  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a fake repo with a tickets/ dir, tickets-index.md, and progress/."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    (repo / "orchestrator").mkdir()
    tickets = repo / "orchestrator" / "tickets"
    tickets.mkdir()
    progress = repo / "orchestrator" / "progress"
    progress.mkdir()
    state = repo / "orchestrator" / "state"
    state.mkdir()

    index = repo / "orchestrator" / "tickets-index.md"
    index.write_text(
        "## L113 cascade delta (test fixture)\n\n"
        "| Ticket | Status | Priority | Title | Created | Completed | Path |\n"
        "|--------|--------|----------|-------|---------|-----------|--------|\n"
    )

    # Monkey-patch the constants in auto_reconcile so it operates on fake_repo
    monkeypatch.setattr(auto_reconcile, "TICKETS_ROOT", tickets)
    monkeypatch.setattr(auto_reconcile, "INDEX_PATH", index)
    monkeypatch.setattr(auto_reconcile, "PROGRESS_DIR", progress)
    monkeypatch.setattr(auto_reconcile, "STATE_DIR", state)
    monkeypatch.setattr(auto_reconcile, "RECONCILE_EVENTS_PATH",
                        progress / ".reconcile_events.jsonl")
    monkeypatch.setattr(auto_reconcile, "RECONCILE_CHECKPOINT_PATH",
                        progress / ".reconcile_checkpoint")

    return repo


def _make_ticket(repo, ticket_id: str, status: str = "DONE") -> Path:
    """Write a ticket file under tickets/ with the given id+status."""
    tickets = repo / "orchestrator" / "tickets"
    path = tickets / f"{ticket_id}.md"
    path.write_text(
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Test ticket {ticket_id}\n"
        f"status: {status}\n"
        f"priority: P1\n"
        f"created: 2026-09-29\n"
        f"updated: 2026-09-29\n"
        f"---\n\n"
        f"# Body of {ticket_id}\n"
    )
    return path


def test_incremental_no_events_returns_zero(fake_repo, capsys):
    """When no events file exists, --incremental returns 0."""
    n = auto_reconcile.run_incremental(dry_run=True)
    assert n == 0
    captured = capsys.readouterr()
    assert "No reconcile events pending" in captured.out


def test_incremental_processes_single_event(fake_repo):
    """Single event in queue → 1 cascade row appended + file truncated."""
    _make_ticket(fake_repo, "TKT-TEST-001")
    ev = {
        "ticket_id": "TKT-TEST-001",
        "fencing_token": 42,
        "status": "DONE",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "holder_id": "test-holder",
        "worker_result_path": None,
        "ts": "2026-09-29T15:00:00Z",
    }
    ev["event_id"] = auto_reconcile.compute_event_id(ev)
    auto_reconcile.append_reconcile_event(ev)

    n = auto_reconcile.run_incremental(dry_run=False)
    assert n == 1

    # Index should have a new cascade row wrapped in markers
    idx_text = (fake_repo / "orchestrator" / "tickets-index.md").read_text()
    assert "<!-- cascade-row-auto -->" in idx_text
    assert "TKT-TEST-001" in idx_text

    # Events file should be empty (truncated)
    events_path = fake_repo / "orchestrator" / "progress" / ".reconcile_events.jsonl"
    assert events_path.stat().st_size == 0

    # Checkpoint should be persisted
    checkpoint_path = fake_repo / "orchestrator" / "progress" / ".reconcile_checkpoint"
    assert checkpoint_path.exists()


def test_incremental_dedupes_same_event(fake_repo):
    """Same event twice in the queue → applied once."""
    _make_ticket(fake_repo, "TKT-TEST-002")
    ev = {
        "ticket_id": "TKT-TEST-002",
        "fencing_token": 99,
        "status": "DONE",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "holder_id": "test-holder",
        "worker_result_path": None,
        "ts": "2026-09-29T15:00:00Z",
    }
    ev["event_id"] = auto_reconcile.compute_event_id(ev)
    # Enqueue twice (same event_id)
    auto_reconcile.append_reconcile_event(ev)
    auto_reconcile.append_reconcile_event(ev)

    n = auto_reconcile.run_incremental(dry_run=False)
    # Should report "skipped 1 duplicates" but still apply the event once
    assert n == 1

    # Index should have exactly ONE auto-row for TKT-TEST-002 — counted
    # by counting the marker-pair occurrences, not the ticket-id text
    # (the id appears 3 times in a row: in id col, in title, and in path).
    idx_text = (fake_repo / "orchestrator" / "tickets-index.md").read_text()
    # The auto-row pattern: <!-- cascade-row-auto -->\n<row>\n<!-- cascade-row-auto -->
    # We split on the marker and count non-empty rows in between.
    parts = idx_text.split("<!-- cascade-row-auto -->")
    auto_rows = [p for p in parts[1::2] if p.strip()]  # odd-indexed, non-empty
    matching = [
        r for r in auto_rows if r.startswith("\n| TKT-TEST-002 |")
    ]
    assert len(matching) == 1, (
        f"expected 1 auto-row for TKT-TEST-002, got {len(matching)}: "
        f"{matching}"
    )


def test_incremental_multiple_events(fake_repo):
    """5 events → 5 cascade rows appended + file truncated."""
    for i in range(5):
        tid = f"TKT-TEST-MULTI-{i:03d}"
        _make_ticket(fake_repo, tid)
        ev = {
            "ticket_id": tid,
            "fencing_token": 100 + i,
            "status": "DONE",
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "holder_id": "test-holder",
            "worker_result_path": None,
            "ts": f"2026-09-29T15:00:{i:02d}Z",
        }
        ev["event_id"] = auto_reconcile.compute_event_id(ev)
        auto_reconcile.append_reconcile_event(ev)

    n = auto_reconcile.run_incremental(dry_run=False)
    assert n == 5

    idx_text = (fake_repo / "orchestrator" / "tickets-index.md").read_text()
    for i in range(5):
        tid = f"TKT-TEST-MULTI-{i:03d}"
        assert tid in idx_text


def test_incremental_updates_at_a_glance_done_count(fake_repo, monkeypatch):
    """At-a-glance DONE count is updated based on filesystem truth."""
    # Set up: 3 DONE tickets on disk
    for i in range(3):
        _make_ticket(fake_repo, f"TKT-TEST-COUNT-{i:03d}", status="DONE")
    # Index has wrong at-a-glance count (999)
    index = fake_repo / "orchestrator" / "tickets-index.md"
    original = index.read_text()
    index.write_text(original + "\n| ✅ DONE | **999** |\n")

    # Enqueue + process
    ev = {
        "ticket_id": "TKT-TEST-COUNT-000",
        "fencing_token": 1,
        "status": "DONE",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "holder_id": "test-holder",
        "worker_result_path": None,
        "ts": "2026-09-29T15:00:00Z",
    }
    ev["event_id"] = auto_reconcile.compute_event_id(ev)
    auto_reconcile.append_reconcile_event(ev)
    auto_reconcile.run_incremental(dry_run=False)

    # Count should now be 3 (the filesystem truth)
    new_text = index.read_text()
    assert "| ✅ DONE | **3** |" in new_text


def test_incremental_handles_missing_ticket_gracefully(fake_repo, capsys):
    """Event for a ticket that doesn't exist → skip with warning, no crash."""
    ev = {
        "ticket_id": "TKT-DOES-NOT-EXIST",
        "fencing_token": 1,
        "status": "DONE",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "holder_id": "test-holder",
        "worker_result_path": None,
        "ts": "2026-09-29T15:00:00Z",
    }
    ev["event_id"] = auto_reconcile.compute_event_id(ev)
    auto_reconcile.append_reconcile_event(ev)

    n = auto_reconcile.run_incremental(dry_run=False)
    # Returns 0 because the ticket wasn't found (event skipped)
    assert n == 1  # event was processed, even if it was a skip

    captured = capsys.readouterr()
    assert "not found on disk" in captured.out