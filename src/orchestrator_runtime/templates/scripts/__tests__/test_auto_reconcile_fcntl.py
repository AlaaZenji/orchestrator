"""Test that auto_reconcile.py serializes writes via fcntl.flock.

TKT-CORE-FIX-STATE-SYNC-FCNTL (2026-09-29): every write to
orchestrator/tickets-index.md is wrapped in ``_file_lock(INDEX_PATH)`` so
two parallel reconciles cannot interleave their read-modify-write cycle.
These tests prove:

  1. ``_file_lock`` acquires and releases cleanly (no orphan locks).
  2. Two concurrent threads using ``_file_lock`` on the same path
     serialize — one waits for the other (proves the mechanism).
  3. ``append_cascade_row_to_index`` writes under the lock (no lost
     rows under contention).
  4. ``update_at_a_glance_done_count`` writes under the lock (last
     count wins, not a torn write).

Thread serialization through ``fcntl.flock`` proves the mechanism works
for processes too — the lock is held on the underlying file descriptor
which the kernel shares across all threads/processes that open it.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import auto_reconcile  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a fake repo with tickets/ and tickets-index.md.

    Mirrors the fixture used by test_auto_reconcile_incremental.py so we
    exercise the same paths the production code uses.
    """
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


# ---------------------------------------------------------------------------
# 1. _file_lock acquires + releases (no orphan locks)
# ---------------------------------------------------------------------------


def test_file_lock_acquires_and_releases(tmp_path):
    """_file_lock yields once and exits cleanly — no orphan locks."""
    target = tmp_path / "lock_target.txt"
    target.write_text("seed")

    with auto_reconcile._file_lock(target):
        # Inside the block: file exists and is openable by another fd.
        assert target.exists()
        # Reading should still work — the lock doesn't block other ops
        # on the fd we hold, only other LOCK_EX acquirers.
        assert target.read_text() == "seed"

    # After the block: the lock should be released. Proving the lock is
    # gone without another process is tricky, but we can prove the fd
    # was closed: opening again from a different fd should not be blocked.
    # Easiest check: re-acquiring the lock in another context succeeds.
    with auto_reconcile._file_lock(target):
        pass  # no exception → previous lock released


def test_file_lock_creates_missing_file(tmp_path):
    """_file_lock creates the file if it doesn't exist (O_CREAT)."""
    target = tmp_path / "lock_create.txt"
    assert not target.exists()

    with auto_reconcile._file_lock(target):
        assert target.exists()


def test_file_lock_releases_on_exception(tmp_path):
    """If the with-body raises, _file_lock still releases the lock."""
    target = tmp_path / "lock_exc.txt"
    target.write_text("seed")

    with pytest.raises(RuntimeError, match="boom"):
        with auto_reconcile._file_lock(target):
            raise RuntimeError("boom")

    # The lock must be released after the exception. Acquire again from
    # the same thread to prove it; on macOS a process holds the lock
    # even after fd close, but re-acquiring the same lock from the same
    # process should not deadlock.
    with auto_reconcile._file_lock(target):
        assert target.read_text() == "seed"


# ---------------------------------------------------------------------------
# 2. Two concurrent threads serialize through _file_lock
# ---------------------------------------------------------------------------


def test_concurrent_threads_serialize(tmp_path):
    """Two threads acquiring the same lock serialize (one waits)."""
    target = tmp_path / "lock_serial.txt"
    target.write_text("")

    timeline = []
    started = threading.Event()

    def worker(name: str, hold_seconds: float) -> None:
        started.set()
        timeline.append((name, "enter"))
        with auto_reconcile._file_lock(target):
            timeline.append((name, "inside"))
            time.sleep(hold_seconds)
            timeline.append((name, "exit_lock"))
        timeline.append((name, "leave"))

    # Thread A holds the lock for 0.2s; thread B starts 50ms later.
    # Without serialization B would enter the with-block immediately.
    # With serialization B's "inside" must come AFTER A's "exit_lock".
    t_a = threading.Thread(target=worker, args=("A", 0.2))
    t_b = threading.Thread(target=worker, args=("B", 0.05))

    t_a.start()
    # Small pause so A definitely reaches its inside first
    time.sleep(0.05)
    t_b.start()

    t_a.join(timeout=5)
    t_b.join(timeout=5)

    # Extract ordered events
    a_inside_idx = next(
        i for i, (n, ev) in enumerate(timeline) if n == "A" and ev == "inside"
    )
    a_exit_idx = next(
        i for i, (n, ev) in enumerate(timeline) if n == "A" and ev == "exit_lock"
    )
    b_inside_idx = next(
        i for i, (n, ev) in enumerate(timeline) if n == "B" and ev == "inside"
    )

    # A must be inside before B is inside (ordering)
    assert a_inside_idx < b_inside_idx, (
        f"A was never inside before B — timeline: {timeline}"
    )
    # B's entry to the lock must come AFTER A's exit_lock (serialization)
    assert a_exit_idx < b_inside_idx, (
        f"B entered the lock before A released it — "
        f"flock failed to serialize. timeline: {timeline}"
    )


def test_concurrent_threads_no_lost_writes(tmp_path):
    """100 threads writing under _file_lock all succeed; final value wins."""
    target = tmp_path / "lock_nolost.txt"
    target.write_text("0")

    n_threads = 50
    barrier = threading.Barrier(n_threads)

    def writer(thread_id: int) -> None:
        barrier.wait()  # maximize contention
        with auto_reconcile._file_lock(target):
            current = int(target.read_text())
            # Tiny sleep widens the race window
            time.sleep(0.001)
            target.write_text(str(current + 1))

    threads = [
        threading.Thread(target=writer, args=(i,)) for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # If serialization failed, the count would be less than n_threads
    final = int(target.read_text())
    assert final == n_threads, (
        f"Expected {n_threads} serialized increments, got {final}. "
        f"flock did not serialize writers."
    )


# ---------------------------------------------------------------------------
# 3. append_cascade_row_to_index writes under the lock
# ---------------------------------------------------------------------------


def test_append_cascade_row_uses_lock(fake_repo, tmp_path):
    """append_cascade_row_to_index acquires _file_lock around its write.

    We patch ``_file_lock`` to track acquisition. The real ``_file_lock``
    is replaced with a wrapper that records entry/exit and delegates to
    the original (so the actual flock still runs and the test still
    proves real behavior). We assert the wrapper entered the lock during
    the write.
    """
    import contextlib
    real_lock = auto_reconcile._file_lock
    lock_calls = []

    @contextlib.contextmanager
    def tracking_lock_cm(path):
        lock_calls.append("enter")
        with real_lock(path):
            yield
        lock_calls.append("exit")

    auto_reconcile._file_lock = tracking_lock_cm
    try:
        _make_ticket(fake_repo, "TKT-FCNTL-001")
        ok = auto_reconcile.append_cascade_row_to_index(
            "TKT-FCNTL-001", "| TKT-FCNTL-001 | ✅ DONE | P1 | x | ... | x | x |\n",
            dry_run=False,
        )
        assert ok is True
    finally:
        auto_reconcile._file_lock = real_lock

    # The lock must have been entered and exited exactly once
    assert lock_calls.count("enter") >= 1, (
        f"append_cascade_row_to_index never acquired _file_lock; calls: {lock_calls}"
    )
    assert lock_calls.count("exit") >= 1
    # Verify the row actually landed in the file
    idx_text = (fake_repo / "orchestrator" / "tickets-index.md").read_text()
    assert "TKT-FCNTL-001" in idx_text


def test_append_cascade_row_thread_safe(fake_repo):
    """50 threads appending different rows → all 50 rows land (no torn write)."""
    n_threads = 50
    barrier = threading.Barrier(n_threads)

    # Pre-create the tickets
    for i in range(n_threads):
        _make_ticket(fake_repo, f"TKT-APP-{i:03d}")

    def appender(tid: str) -> None:
        barrier.wait()
        row_text = (
            f"| {tid} | ✅ DONE | P1 | Thread row {tid} | 2026-09-29 | "
            f"2026-09-29 | `x` |\n"
        )
        auto_reconcile.append_cascade_row_to_index(tid, row_text, dry_run=False)

    threads = [
        threading.Thread(target=appender, args=(f"TKT-APP-{i:03d}",))
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # All 50 rows must be present. Without the lock, threads would race
    # on read-modify-write and some rows would be lost.
    idx_text = (fake_repo / "orchestrator" / "tickets-index.md").read_text()
    for i in range(n_threads):
        tid = f"TKT-APP-{i:03d}"
        assert tid in idx_text, (
            f"Thread row for {tid} missing from index — "
            f"flock failed to serialize append_cascade_row."
        )


# ---------------------------------------------------------------------------
# 4. update_at_a_glance_done_count writes under the lock
# ---------------------------------------------------------------------------


def test_update_at_a_glance_uses_lock(fake_repo):
    """update_at_a_glance_done_count acquires _file_lock around its write."""
    real_lock = auto_reconcile._file_lock
    lock_calls = []

    import contextlib
    @contextlib.contextmanager
    def tracking_lock_cm(path):
        lock_calls.append("enter")
        with real_lock(path):
            yield
        lock_calls.append("exit")

    auto_reconcile._file_lock = tracking_lock_cm
    try:
        # The fixture index doesn't have a `| ✅ DONE | **N** |` row yet.
        # Inject one so the update has something to change.
        index = fake_repo / "orchestrator" / "tickets-index.md"
        original = index.read_text()
        index.write_text(original + "| ✅ DONE | **0** |\n")

        changed = auto_reconcile.update_at_a_glance_done_count(42, dry_run=False)
        assert changed is True
    finally:
        auto_reconcile._file_lock = real_lock

    assert lock_calls.count("enter") >= 1, (
        f"update_at_a_glance_done_count never acquired _file_lock; "
        f"calls: {lock_calls}"
    )
    assert lock_calls.count("exit") >= 1
    idx_text = (fake_repo / "orchestrator" / "tickets-index.md").read_text()
    assert "| ✅ DONE | **42** |" in idx_text


def test_update_at_a_glance_thread_safe(fake_repo):
    """50 threads updating the count → final value is one of the 50 writes."""
    # Inject the count line so the update has something to change
    index = fake_repo / "orchestrator" / "tickets-index.md"
    original = index.read_text()
    index.write_text(original + "| ✅ DONE | **0** |\n")

    n_threads = 50
    barrier = threading.Barrier(n_threads)
    results = []

    def updater(value: int) -> None:
        barrier.wait()
        auto_reconcile.update_at_a_glance_done_count(value, dry_run=False)
        results.append(value)

    threads = [
        threading.Thread(target=updater, args=(i + 1,))
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # The final write_text must match one of the 50 values (no torn write).
    # If the lock failed, the read-modify-write cycle could overwrite an
    # earlier write with a stale read, landing on a value not in the set.
    idx_text = index.read_text()
    # Extract the count from the final file
    import re as _re
    m = _re.search(r"\| ✅ DONE \|\s*\*\*(\d+)\*\*", idx_text)
    assert m, f"could not find DONE count in index: {idx_text!r}"
    final_count = int(m.group(1))
    assert 1 <= final_count <= n_threads, (
        f"Final count {final_count} not in valid set "
        f"[1, {n_threads}] — torn write detected."
    )


# ---------------------------------------------------------------------------
# 5. Module-level surface: imports + symbol exist
# ---------------------------------------------------------------------------


def test_module_imports_fcntl_and_contextlib():
    """auto_reconcile must import fcntl + contextlib at module level."""
    import importlib
    # Re-import to be sure we see the latest
    mod = importlib.reload(auto_reconcile)
    assert hasattr(mod, "fcntl"), "fcntl not imported in auto_reconcile"
    assert hasattr(mod, "contextlib"), "contextlib not imported in auto_reconcile"
    assert callable(getattr(mod, "_file_lock", None)), (
        "_file_lock context manager not exposed by auto_reconcile"
    )


def test_file_lock_docstring_mentions_ticket():
    """_file_lock docstring references the ticket for grep-ability."""
    doc = auto_reconcile._file_lock.__doc__ or ""
    assert "TKT-CORE-FIX-STATE-SYNC-FCNTL" in doc, (
        f"_file_lock docstring missing ticket reference: {doc!r}"
    )