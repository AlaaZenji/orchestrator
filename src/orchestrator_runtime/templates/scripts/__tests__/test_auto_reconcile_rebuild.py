"""Test the --rebuild mode of auto_reconcile.py.

TKT-CORE-FIX-STATE-SYNC (2026-09-29): disaster-recovery primitive.
Walks every ticket frontmatter on disk and regenerates tickets-index.md
at-a-glance row + cascade delta rows. Preserves any existing rich
narrative rows (detected by lack of the <!-- cascade-row-auto --> marker).
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import auto_reconcile  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a fake repo with tickets/ and tickets-index.md."""
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


def test_rebuild_corrects_at_a_glance_count(fake_repo):
    """At-a-glance count is corrected to match filesystem truth."""
    # 3 DONE on disk
    for i in range(3):
        _make_ticket(fake_repo, f"TKT-REBUILD-{i:03d}", status="DONE")

    # Index has wrong count
    index = fake_repo / "orchestrator" / "tickets-index.md"
    original = index.read_text()
    index.write_text(original + "\n| ✅ DONE | **999** |\n")

    # Run --rebuild
    auto_reconcile.run_rebuild(dry_run=False)

    new_text = index.read_text()
    # Count should now be 3
    assert "| ✅ DONE | **3** |" in new_text


def test_rebuild_preserves_handwritten_narrative(fake_repo):
    """Hand-written narrative rows (no marker) are preserved verbatim."""
    # Pre-populate the index with a hand-written narrative row
    index = fake_repo / "orchestrator" / "tickets-index.md"
    handwritten = (
        "## L113 cascade delta (test fixture)\n\n"
        "| Ticket | Status | Priority | Title | Created | Completed | Path |\n"
        "|--------|--------|----------|-------|---------|-----------|--------|\n"
        "| TKT-HAND-WRITTEN | ✅ DONE | P0 | **L113 worker burn — "
        "this is rich hand-written narrative that should be preserved verbatim.** "
        "Verifier `abc123def45678901` PASS. | 2026-09-29 | 2026-09-29 | "
        "`orchestrator/tickets/foo.md` |\n"
    )
    index.write_text(handwritten)

    # Add 2 DONE tickets on disk
    for i in range(2):
        _make_ticket(fake_repo, f"TKT-REBUILD-PARTIAL-{i:03d}", status="DONE")

    auto_reconcile.run_rebuild(dry_run=False)

    new_text = index.read_text()
    # The hand-written row should be preserved verbatim
    assert "this is rich hand-written narrative that should be preserved verbatim" in new_text
    assert "Verifier `abc123def45678901` PASS" in new_text
    # The auto-generated rows for the 2 disk tickets should be added
    assert "TKT-REBUILD-PARTIAL-000" in new_text
    assert "TKT-REBUILD-PARTIAL-001" in new_text
    # And they should be wrapped in markers
    assert "<!-- cascade-row-auto -->" in new_text


def test_rebuild_dry_run_does_not_write(fake_repo):
    """--rebuild --dry-run prints but doesn't modify tickets-index.md."""
    for i in range(2):
        _make_ticket(fake_repo, f"TKT-DRYRUN-{i:03d}", status="DONE")

    index = fake_repo / "orchestrator" / "tickets-index.md"
    before = index.read_text()

    auto_reconcile.run_rebuild(dry_run=True)
    after = index.read_text()

    # File should be byte-identical
    assert before == after


def test_rebuild_handles_no_cascade_section(fake_repo):
    """When index has no ## L### heading, --rebuild appends to end."""
    # Wipe the cascade section
    index = fake_repo / "orchestrator" / "tickets-index.md"
    index.write_text("# Top-level heading\n\nNo cascade sections here.\n")

    _make_ticket(fake_repo, "TKT-NO-SECTION-001", status="DONE")

    auto_reconcile.run_rebuild(dry_run=False)

    new_text = index.read_text()
    # The ticket should still be appended (graceful fallback)
    assert "TKT-NO-SECTION-001" in new_text


def test_rebuild_matches_frontmatter_truth(fake_repo):
    """After --rebuild, the at-a-glance count matches the actual frontmatter truth."""
    # Mix of statuses: 2 DONE, 1 PARTIAL, 1 QUEUED
    _make_ticket(fake_repo, "TKT-MIX-DONE-1", status="DONE")
    _make_ticket(fake_repo, "TKT-MIX-DONE-2", status="DONE")
    _make_ticket(fake_repo, "TKT-MIX-PARTIAL", status="PARTIAL")
    _make_ticket(fake_repo, "TKT-MIX-QUEUED", status="QUEUED")

    auto_reconcile.run_rebuild(dry_run=False)

    counts = auto_reconcile.count_tickets_by_status()
    assert counts["DONE"] == 2
    assert counts["PARTIAL"] == 1
    assert counts["QUEUED"] == 1


def test_rebuild_strips_existing_auto_rows(fake_repo):
    """--rebuild strips existing auto-rows before regenerating."""
    _make_ticket(fake_repo, "TKT-STRIP-001", status="DONE")

    # Pre-populate with a stale auto-row for a different ticket
    index = fake_repo / "orchestrator" / "tickets-index.md"
    index.write_text(
        "## L113 cascade delta (test)\n\n"
        "<!-- cascade-row-auto -->\n"
        "| TKT-STALE | ✅ DONE | P2 | Stale row that should be stripped. | "
        "2026-09-29 | 2026-09-29 | `x` |\n"
        "<!-- cascade-row-auto -->\n"
    )

    auto_reconcile.run_rebuild(dry_run=False)

    new_text = index.read_text()
    # The stale row is gone
    assert "TKT-STALE" not in new_text
    # The real ticket row is added
    assert "TKT-STRIP-001" in new_text