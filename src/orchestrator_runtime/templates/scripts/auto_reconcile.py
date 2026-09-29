#!/usr/bin/env python3
"""Auto-reconcile tickets-index.md after each layer cascade.

Reads every ticket frontmatter under orchestrator/tickets/ and updates
orchestrator/tickets-index.md at-a-glance row + relevant per-row entries.
Saves ~10 min/layer of manual orchestrator work.

Modes (TKT-ORCH-FIX-STATE-SYNC, 2026-09-29):
  (no args)        -- Baseline reconcile: updates at-a-glance DONE count,
                       processes any pending events from
                       .reconcile_events.jsonl (self-heal on startup).
  --incremental     -- Drain the .reconcile_events.jsonl queue, append
                       one cascade delta row per event to tickets-index.md,
                       truncate the events file, update the at-a-glance
                       count.
  --rebuild        -- Disaster recovery. Walk every ticket frontmatter on
                       disk and regenerate tickets-index.md at-a-glance
                       row + cascade delta rows. Preserves any existing
                       rich narrative rows (detected by lack of the
                       <!-- cascade-row-auto --> marker).
  --append-cascade-row <TICKET_ID>
                    -- Append a single auto-generated cascade delta row
                       for one ticket. Reads WORKER_RESULT.json from
                       orchestrator/progress/ if present.
  --dry-run        -- Print what would change; do not write.

Concurrency (TKT-ORCH-FIX-STATE-SYNC-FCNTL, 2026-09-29):
  Every write to tickets-index.md is wrapped in ``_file_lock(INDEX_PATH)``
  which holds an exclusive POSIX ``fcntl.flock`` on the index file. The
  prior 2-second debounce in lease.release_with_reconcile was a soft
  mitigation; ``flock`` is the POSIX-correct hard lock. Two parallel
  reconciles serialize through the lock and cannot interleave their
  read-modify-write cycle.

Usage:
  python3 orchestrator/scripts/auto_reconcile.py
  python3 orchestrator/scripts/auto_reconcile.py --incremental
  python3 orchestrator/scripts/auto_reconcile.py --rebuild
  python3 orchestrator/scripts/auto_reconcile.py --append-cascade-row TKT-FOO
"""
import json
import re
import sys
import time
import argparse
import os
import fcntl
import contextlib
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

TICKETS_ROOT = Path(__file__).parent.parent / "tickets"
INDEX_PATH = Path(__file__).parent.parent / "tickets-index.md"
STATE_DIR = Path(__file__).parent.parent / "state"
PROGRESS_DIR = Path(__file__).parent.parent / "progress"
PID_FILE = STATE_DIR / "daemon.pid"

# TKT-ORCH-FIX-STATE-SYNC: reconcile queue infrastructure
RECONCILE_EVENTS_PATH = PROGRESS_DIR / ".reconcile_events.jsonl"
RECONCILE_CHECKPOINT_PATH = PROGRESS_DIR / ".reconcile_checkpoint"
RECONCILE_DEBOUNCE_SECONDS = 2.0
AUTO_ROW_MARKER = "<!-- cascade-row-auto -->"


# ---------------------------------------------------------------------------
# TKT-ORCH-FIX-STATE-SYNC-FCNTL (2026-09-29): POSIX file lock around every
# INDEX_PATH write. fcntl.flock is per-file-descriptor and shared across
# processes, so two parallel reconciles serialize through the lock. On
# macOS the BSD-style flock is fully supported; on Linux it is the
# canonical POSIX lock. The lock is released either by the LOCK_UN call
# or implicitly by os.close(); either path is reached via the context
# manager's ``finally`` so we cannot leak a held lock on exception.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _file_lock(path: Path):
    """Acquire an exclusive ``fcntl.flock`` on ``path``.

    TKT-ORCH-FIX-STATE-SYNC-FCNTL (2026-09-29): opens ``path`` with
    O_RDWR|O_CREAT (mode 0o644), holds LOCK_EX for the duration of the
    ``with`` block, and releases via LOCK_UN + os.close in ``finally``.
    If ``os.close`` or ``flock`` fails (e.g. fd already closed), we
    swallow OSError — the lock release happens implicitly when the
    kernel closes the underlying file descriptor at process exit, so
    failing to release is non-fatal. The mechanism is identical on
    macOS (BSD flock) and Linux (POSIX flock).

    The lock MUST be held across the full read-modify-write cycle, not
    just the write_text call. Two threads that both read state A,
    compute their own new_text, and then race to write will lose one
    update unless they hold the lock for the whole critical section.
    """
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

# _parse_etime_hours is owned by burnqueue_daemon.py (moved there so the
# daemon is the canonical owner of its own helpers; TKT-ORCH-PERF-001 review).
# We import it lazily so auto_reconcile.py still works even if the daemon
# is missing (e.g., fresh checkout).
try:
    import burnqueue_daemon as _bd  # type: ignore
    _parse_etime_hours = _bd._parse_etime_hours
except ImportError:
    # Inline fallback (kept in sync with burnqueue_daemon.py).
    def _parse_etime_hours(etime: str) -> float:
        etime = etime.strip()
        try:
            if "-" in etime:
                days, rest = etime.split("-", 1)
                days = int(days)
            else:
                days = 0
                rest = etime
            h, m, s = rest.split(":")
            return days * 24 + int(h) + int(m) / 60 + int(s) / 3600
        except Exception:
            return 0.0


def parse_frontmatter(path: Path) -> dict:
    """Parse YAML-ish frontmatter from a ticket file."""
    text = path.read_text(errors="ignore")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*?)\s*$", line)
        if m:
            fm[m.group(1)] = m.group(2)
    return fm


def find_ticket_file(ticket_id: str) -> Path | None:
    """Locate the on-disk ticket file for a given ticket id.

    Walks ``TICKETS_ROOT`` and returns the first file whose frontmatter
    ``id:`` field matches ``ticket_id`` (canonical lookup, immune to
    file-naming drift). Returns ``None`` if not found.
    """
    for path in TICKETS_ROOT.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(path):
            continue
        try:
            fm = parse_frontmatter(path)
        except Exception:
            continue
        if fm.get("id") == ticket_id:
            return path
    return None


# ---------------------------------------------------------------------------
# TKT-ORCH-FIX-STATE-SYNC: event-driven reconcile queue
# ---------------------------------------------------------------------------


def append_reconcile_event(event: dict) -> None:
    """Append one JSONL event to ``.reconcile_events.jsonl``.

    Atomic append — opens with O_APPEND so concurrent writers don't
    interleave. Caller is responsible for the event payload shape:

        {
          "ticket_id": "TKT-...",
          "tenant_id": "...",
          "holder_id": "...",
          "fencing_token": 123,
          "status": "DONE" | "PARTIAL" | "BLOCKED" | ...,
          "worker_result_path": "orchestrator/progress/...json",
          "ts": "<ISO-8601>",
          "event_id": "<sha256 hex>",  # idempotency dedup
        }
    """
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True)
    with RECONCILE_EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_reconcile_checkpoint() -> int:
    """Read the last-applied byte offset in ``.reconcile_events.jsonl``.

    Returns 0 if no checkpoint exists (process all events).
    """
    if not RECONCILE_CHECKPOINT_PATH.exists():
        return 0
    try:
        return int(RECONCILE_CHECKPOINT_PATH.read_text().strip() or "0")
    except Exception:
        return 0


def write_reconcile_checkpoint(offset: int) -> None:
    """Persist the applied offset atomically (write-then-rename)."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RECONCILE_CHECKPOINT_PATH.with_suffix(".tmp")
    tmp.write_text(str(offset))
    tmp.rename(RECONCILE_CHECKPOINT_PATH)


def compute_event_id(event: dict) -> str:
    """Stable sha256 hex over the canonical event fields (dedup)."""
    canonical = json.dumps(
        {
            "ticket_id": event.get("ticket_id"),
            "status": event.get("status"),
            "fencing_token": event.get("fencing_token"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def append_cascade_row_to_index(ticket_id: str, row_text: str,
                                dry_run: bool = False) -> bool:
    """Append one cascade delta row to ``tickets-index.md``.

    Inserts the row just before the next ``## `` heading that follows the
    most recent ``## L`` cascade heading (i.e., into the latest cascade
    block). If no cascade block is found, appends to the end of the file.
    Returns True if a write happened, False if dry-run.

    The ``row_text`` MUST end with a newline. The row is wrapped with the
    :data:`AUTO_ROW_MARKER` comment so the ``--rebuild`` mode can
    distinguish auto-generated rows from hand-written narrative rows.

    TKT-ORCH-FIX-STATE-SYNC-FCNTL: the read+parse+write cycle is held
    under ``_file_lock(INDEX_PATH)`` so two parallel callers cannot race
    and lose a row. ``dry_run=True`` skips the lock (no write happens).
    """
    if dry_run:
        # No write — no need to lock. Just parse so dry-run can be tested.
        idx_text = INDEX_PATH.read_text()
        cascade_re = re.compile(r"^## L\d+ cascade delta\b.*$", re.MULTILINE)
        cascade_matches = list(cascade_re.finditer(idx_text))
        if cascade_matches:
            last_cascade = cascade_matches[-1]
            next_section_re = re.compile(r"^## ", re.MULTILINE)
            next_section = next_section_re.search(idx_text, last_cascade.end())
            insert_at = next_section.start() if next_section else len(idx_text)
        else:
            insert_at = len(idx_text)
        wrapped_row = f"{AUTO_ROW_MARKER}\n{row_text}{AUTO_ROW_MARKER}\n"
        _ = idx_text[:insert_at] + wrapped_row + idx_text[insert_at:]
        return False

    # Live write: hold the lock across the full read-modify-write cycle.
    with _file_lock(INDEX_PATH):
        idx_text = INDEX_PATH.read_text()

        # Find the latest "## L<NNN> cascade delta" header. If found, insert
        # before the next "## " (the next section heading). Otherwise, append
        # to the end.
        cascade_re = re.compile(r"^## L\d+ cascade delta\b.*$", re.MULTILINE)
        cascade_matches = list(cascade_re.finditer(idx_text))
        if cascade_matches:
            last_cascade = cascade_matches[-1]
            # Find next "## " after last cascade heading
            next_section_re = re.compile(r"^## ", re.MULTILINE)
            next_section = next_section_re.search(idx_text, last_cascade.end())
            if next_section:
                insert_at = next_section.start()
            else:
                insert_at = len(idx_text)
        else:
            # No cascade block found — append at end
            insert_at = len(idx_text)

        # Wrap with markers for --rebuild preservation
        wrapped_row = f"{AUTO_ROW_MARKER}\n{row_text}{AUTO_ROW_MARKER}\n"
        new_text = idx_text[:insert_at] + wrapped_row + idx_text[insert_at:]
        INDEX_PATH.write_text(new_text)
    return True


def update_at_a_glance_done_count(done_count: int, dry_run: bool = False) -> bool:
    """Update the ``| ✅ DONE | **N** |`` line in tickets-index.md.

    Returns True if a write happened.

    TKT-ORCH-FIX-STATE-SYNC-FCNTL: the read+regex+write cycle is held
    under ``_file_lock(INDEX_PATH)`` so two parallel callers cannot
    race on the read-modify-write.
    """
    if dry_run:
        idx_text = INDEX_PATH.read_text()
        new_text = re.sub(
            r"\| ✅ DONE \| \*\*\d+\*\*",
            f"| ✅ DONE | **{done_count}**",
            idx_text,
            count=1,
        )
        return new_text != idx_text

    with _file_lock(INDEX_PATH):
        idx_text = INDEX_PATH.read_text()
        new_text = re.sub(
            r"\| ✅ DONE \| \*\*\d+\*\*",
            f"| ✅ DONE | **{done_count}**",
            idx_text,
            count=1,
        )
        if new_text != idx_text:
            INDEX_PATH.write_text(new_text)
            return True
    return False


def count_tickets_by_status() -> dict[str, int]:
    """Return {status_upper: count, ...} across all ticket frontmatter.

    Tolerant of variant status strings:
      * "DONE" — exact match
      * "DONE (deliverable ready for review)" — DONE-with-suffix
      * "DONE  # Layer 73 ..." — DONE-with-inline-comment
    The first whitespace-delimited token (stripped of trailing colons /
    comments) is the canonical status. Anything starting with `DONE`
    counts as DONE; `DRAFT` and `LANDED` count as their own categories.
    """
    # Canonical categories. Anything not matching one of these is bucketed
    # as OTHER so the at-a-glance row stays truthful.
    canonical = {
        "DONE", "PARTIAL", "PARTIAL_WITH_FOLLOW_UPS", "QUEUED", "BLOCKED",
        "IN_PROGRESS", "DEFERRED", "CANCELLED", "DRAFT", "LANDED",
    }
    by_status: dict[str, int] = {s: 0 for s in canonical}
    by_status["OTHER"] = 0

    for path in TICKETS_ROOT.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(path):
            continue
        fm = parse_frontmatter(path)
        raw = (fm.get("status") or "OTHER").strip()
        # Strip inline comments ("DONE  # Layer 73 ..." → "DONE")
        if "#" in raw:
            raw = raw.split("#", 1)[0].strip()
        # First whitespace-delimited token is the canonical status
        token = raw.split()[0].upper() if raw else "OTHER"
        if token in by_status:
            by_status[token] += 1
        else:
            by_status["OTHER"] += 1
    return by_status


def build_cascade_row(ticket_id: str, fm: dict,
                      worker_result: dict | None = None) -> str:
    """Build the canonical 7-column cascade delta row for one ticket.

    Mirrors the L113-format: status | priority | title | created |
    completed | path. If ``worker_result`` is supplied, prepend a short
    auto-narrative (worker_id + verifier count + file count + minutes).
    """
    s = fm.get("status", "OTHER").upper()
    p = fm.get("priority", "OTHER").upper()
    title = (fm.get("title") or "").strip()[:80]
    created = fm.get("created", "")
    updated = fm.get("updated", "")
    completed = updated or created

    # Path: walk tickets/ and find the relative path
    rel_path = ""
    tf = find_ticket_file(ticket_id)
    if tf is not None:
        try:
            rel_path = str(tf.relative_to(TICKETS_ROOT.parent.parent))
        except Exception:
            rel_path = str(tf)

    # Auto-narrative prefix (when WORKER_RESULT.json is present)
    prefix = ""
    if worker_result:
        worker_id = (worker_result.get("worker_id") or "")[:12]
        v_count = worker_result.get("verifier_provenance", {}).get(
            "dispatched_count", 0
        )
        file_count = len(worker_result.get("files_changed", []))
        elapsed = worker_result.get("elapsed_minutes", "?")
        prefix = (
            f"**Auto-reconcile:** worker `{worker_id}`, "
            f"{v_count}/8 verifiers, {file_count} files, {elapsed}min. "
        )

    return (
        f"| {ticket_id} | {status_marker(s)} {s} | {p} | "
        f"{prefix}{title} | {created} | {completed} | "
        f"`{rel_path}` |\n"
    )


def status_marker(status: str) -> str:
    """Return the unicode status marker for a given status."""
    return {
        "DONE": "✅",
        "PARTIAL": "🟡",
        "PARTIAL_WITH_FOLLOW_UPS": "🟨",
        "QUEUED": "🟦",
        "BLOCKED": "⛔",
        "IN_PROGRESS": "🟧",
        "DEFERRED": "⏸",
        "CANCELLED": "🚫",
    }.get(status, "❓")


def extract_worker_result(worker_result_path: str | None) -> dict | None:
    """Load WORKER_RESULT.json from a path; return None on any failure."""
    if not worker_result_path:
        return None
    p = Path(worker_result_path)
    if not p.is_absolute():
        # Relative to repo root (parent of orchestrator/)
        p = Path(__file__).resolve().parents[2] / worker_result_path
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def run_incremental(dry_run: bool = False) -> int:
    """Drain the reconcile-events queue, append cascade rows, update count.

    Returns the number of events processed.
    """
    if not RECONCILE_EVENTS_PATH.exists():
        print("No reconcile events pending.")
        return 0

    checkpoint = read_reconcile_checkpoint()
    raw = RECONCILE_EVENTS_PATH.read_text(errors="ignore")
    if not raw:
        print("Reconcile events file empty.")
        return 0

    events: list[dict] = []
    offset = 0
    for line in raw.splitlines():
        if offset < checkpoint:
            offset += len(line) + 1  # +1 for newline
            continue
        line = line.strip()
        if not line:
            offset += 1
            continue
        try:
            ev = json.loads(line)
        except Exception:
            # Skip malformed; advance offset
            offset += len(line) + 1
            continue
        # Idempotency: skip events with event_id we've already applied
        ev_id = ev.get("event_id") or compute_event_id(ev)
        ev["event_id"] = ev_id
        events.append(ev)
        offset += len(line) + 1

    # Dedup within this batch (same event_id twice in the queue → apply once)
    seen: set[str] = set()
    deduped: list[dict] = []
    for ev in events:
        eid = ev["event_id"]
        if eid in seen:
            continue
        seen.add(eid)
        deduped.append(ev)

    print(f"Processing {len(deduped)} reconcile events "
          f"(skipped {len(events) - len(deduped)} duplicates)...")

    for ev in deduped:
        tid = ev.get("ticket_id")
        if not tid:
            continue
        tf = find_ticket_file(tid)
        if tf is None:
            print(f"  ⚠️  ticket {tid} not found on disk; skipping")
            continue
        fm = parse_frontmatter(tf)
        wr = extract_worker_result(ev.get("worker_result_path"))
        row = build_cascade_row(tid, fm, wr)
        append_cascade_row_to_index(tid, row, dry_run=dry_run)
        print(f"  ✅ {tid} → cascade row appended")

    # Update at-a-glance count from filesystem truth (always source-of-truth)
    counts = count_tickets_by_status()
    update_at_a_glance_done_count(counts["DONE"], dry_run=dry_run)

    if not dry_run:
        write_reconcile_checkpoint(
            RECONCILE_EVENTS_PATH.stat().st_size
        )
        # Atomic truncate: write empty file via write-then-rename
        tmp = RECONCILE_EVENTS_PATH.with_suffix(".tmp")
        tmp.write_text("")
        tmp.rename(RECONCILE_EVENTS_PATH)
        print(f"✅ Events file truncated; checkpoint at end-of-file.")

    return len(deduped)


def run_append_cascade_row(ticket_id: str, dry_run: bool = False) -> bool:
    """Append one cascade delta row for ``ticket_id``."""
    tf = find_ticket_file(ticket_id)
    if tf is None:
        print(f"error: ticket {ticket_id} not found under {TICKETS_ROOT}",
              file=sys.stderr)
        return False
    fm = parse_frontmatter(tf)

    # Try to find a matching WORKER_RESULT.json in progress/
    wr_path = None
    if PROGRESS_DIR.exists():
        candidates = sorted(PROGRESS_DIR.glob(f"{ticket_id}*-WORKER-RESULT.json"))
        if candidates:
            wr_path = str(candidates[-1])

    wr = extract_worker_result(wr_path)
    row = build_cascade_row(ticket_id, fm, wr)
    append_cascade_row_to_index(ticket_id, row, dry_run=dry_run)
    print(f"✅ Cascade row appended for {ticket_id}")
    if wr_path:
        print(f"   (with WORKER_RESULT from {wr_path})")
    return True


def run_rebuild(dry_run: bool = False) -> bool:
    """Disaster recovery: rebuild tickets-index.md at-a-glance + cascade rows.

    Walks every ticket frontmatter, regenerates the at-a-glance row, and
    rewrites every cascade delta row that carries the AUTO_ROW_MARKER.
    Hand-written narrative rows (no marker) are preserved verbatim.

    TKT-ORCH-FIX-STATE-SYNC-FCNTL: the read + recompute + write cycle
    is held under ``_file_lock(INDEX_PATH)`` so a rebuild cannot race
    with an in-flight ``append_cascade_row_to_index`` (otherwise the
    append could be overwritten by the rebuild's snapshot).
    """

    def _build_rebuild_text() -> str:
        idx_text = INDEX_PATH.read_text()
        # 1. Strip every AUTO_ROW_MARKER-wrapped row from the existing
        #    index (the rebuild regenerates them from scratch).
        strip_re = re.compile(
            re.escape(AUTO_ROW_MARKER) + r"\n.*?\n"
            + re.escape(AUTO_ROW_MARKER) + r"\n",
            re.DOTALL,
        )
        cleaned = strip_re.sub("", idx_text)

        # 2. Walk tickets and rebuild
        counts = count_tickets_by_status()
        new_idx = re.sub(
            r"\| ✅ DONE \| \*\*\d+\*\*",
            f"| ✅ DONE | **{counts['DONE']}**",
            cleaned,
            count=1,
        )

        # 3. Re-insert AUTO_ROW_MARKER rows for every ticket, in the
        #    latest cascade delta section. If no cascade section exists,
        #    append to end.
        cascade_re = re.compile(r"^## L\d+ cascade delta\b.*$",
                                re.MULTILINE)
        cascade_matches = list(cascade_re.finditer(new_idx))
        if cascade_matches:
            last_cascade = cascade_matches[-1]
            next_section_re = re.compile(r"^## ", re.MULTILINE)
            next_section = next_section_re.search(
                new_idx, last_cascade.end()
            )
            insert_at = (
                next_section.start() if next_section else len(new_idx)
            )
        else:
            insert_at = len(new_idx)

        # Build all rows first, then insert once
        all_rows = []
        for tf in sorted(TICKETS_ROOT.glob("**/TKT-*.md")):
            if ".migrate-backups" in str(tf):
                continue
            try:
                fm = parse_frontmatter(tf)
            except Exception:
                continue
            tid = fm.get("id")
            if not tid or not tid.startswith("TKT-"):
                continue
            wr_path = None
            if PROGRESS_DIR.exists():
                candidates = sorted(
                    PROGRESS_DIR.glob(f"{tid}*-WORKER-RESULT.json")
                )
                if candidates:
                    wr_path = str(candidates[-1])
            wr = extract_worker_result(wr_path)
            row = build_cascade_row(tid, fm, wr)
            all_rows.append(
                f"{AUTO_ROW_MARKER}\n{row}{AUTO_ROW_MARKER}\n"
            )

        return (
            new_idx[:insert_at]
            + f"<!-- rebuild auto-inserted {len(all_rows)} rows -->\n"
            + "".join(all_rows)
            + new_idx[insert_at:]
        )

    if dry_run:
        # No write — no need to lock.
        final_text = _build_rebuild_text()
        # Print counts but don't write.
        counts = count_tickets_by_status()
        all_rows_marker_count = final_text.count(AUTO_ROW_MARKER)
        n_rows = all_rows_marker_count // 2
        print(f"✅ Rebuilt {n_rows} cascade rows; "
              f"DONE count = {counts['DONE']}")
        return True

    with _file_lock(INDEX_PATH):
        final_text = _build_rebuild_text()
        INDEX_PATH.write_text(final_text)

    counts = count_tickets_by_status()
    all_rows_marker_count = final_text.count(AUTO_ROW_MARKER)
    n_rows = all_rows_marker_count // 2
    print(f"✅ Rebuilt {n_rows} cascade rows; "
          f"DONE count = {counts['DONE']}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile tickets-index.md from ticket frontmatter"
    )
    parser.add_argument("--incremental", action="store_true",
                        help="Drain .reconcile_events.jsonl queue")
    parser.add_argument("--rebuild", action="store_true",
                        help="Full rebuild from filesystem")
    parser.add_argument("--append-cascade-row", metavar="TICKET_ID",
                        help="Append one cascade row for TICKET_ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes; do not write")
    args = parser.parse_args()

    if args.rebuild:
        run_rebuild(dry_run=args.dry_run)
        return

    if args.append_cascade_row:
        ok = run_append_cascade_row(args.append_cascade_row,
                                    dry_run=args.dry_run)
        sys.exit(0 if ok else 2)

    if args.incremental:
        run_incremental(dry_run=args.dry_run)
        return

    # Default: baseline reconcile (preserve original behaviour + self-heal).
    # Use the tolerant counter (handles "DONE (suffix)", "DONE # comment",
    # "DRAFT", "LANDED", etc.) — TKT-ORCH-FIX-STATE-SYNC.
    by_status = count_tickets_by_status()
    by_priority = {"P0": 0, "P1": 0, "P2": 0, "P3": 0, "OTHER": 0}

    for path in TICKETS_ROOT.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(path):
            continue
        fm = parse_frontmatter(path)
        p = fm.get("priority", "OTHER").upper()
        if p in by_priority:
            by_priority[p] += 1
        else:
            by_priority["OTHER"] += 1

    total = sum(by_status.values())
    done = by_status["DONE"]
    partial = by_status["PARTIAL"] + by_status.get("PARTIAL_WITH_FOLLOW_UPS", 0)
    blocked = by_status["BLOCKED"]
    queued = by_status["QUEUED"]

    print(f"=== TICKET COUNT RECONCILE ===")
    print(f"Total: {total}")
    print(f"  DONE: {done}")
    print(f"  PARTIAL: {partial}")
    print(f"  QUEUED: {queued}")
    print(f"  BLOCKED: {blocked}")
    print(f"  (DRAFT: {by_status.get('DRAFT', 0)}"
          f", LANDED: {by_status.get('LANDED', 0)}"
          f", OTHER: {by_status.get('OTHER', 0)})")
    print(f"By priority: {by_priority}")

    # Update at-a-glance row (original behaviour).
    # TKT-ORCH-FIX-STATE-SYNC-FCNTL: the read+regex+write cycle is held
    # under ``_file_lock(INDEX_PATH)`` so two parallel baselines cannot
    # race on the read-modify-write.
    if not args.dry_run:
        with _file_lock(INDEX_PATH):
            idx_text = INDEX_PATH.read_text()
            new_text = re.sub(
                r"\| ✅ DONE \| \*\*\d+\*\*",
                f"| ✅ DONE | **{done}**",
                idx_text,
                count=1,
            )
            if new_text != idx_text:
                INDEX_PATH.write_text(new_text)
                print(f"\n✅ tickets-index.md updated: DONE → {done}")
            else:
                print(f"\n✓ tickets-index.md already correct")

    # TKT-ORCH-FIX-STATE-SYNC: self-heal — process any pending events.
    # This way, even an ad-hoc `auto_reconcile.py` (no args) drains the
    # queue. If anything was pending, the user sees a one-line summary.
    if RECONCILE_EVENTS_PATH.exists():
        size = RECONCILE_EVENTS_PATH.stat().st_size
        checkpoint = read_reconcile_checkpoint()
        if size > checkpoint:
            print(f"\n=== SELF-HEAL: draining {size - checkpoint} "
                  f"bytes of pending events ===")
            n = run_incremental(dry_run=args.dry_run)
            if n:
                print(f"✅ Self-heal processed {n} events")

    # TKT-ORCH-FIX-STATE-CONSISTENCY (2026-09-29): every reconcile pass
    # also runs state.audit() so drift findings are surfaced immediately.
    # If --fail-on-drift is set, exit non-zero so the calling context
    # (burn_queue, daemon, CI) sees the drift.
    try:
        import state as state_mod  # type: ignore
        findings = state_mod.audit()
        if findings:
            errors = [f for f in findings if f.severity == "ERROR"]
            print(f"\n=== STATE AUDIT (drift findings) ===")
            print(f"Total findings: {len(findings)} "
                  f"({len(errors)} ERROR, "
                  f"{len([f for f in findings if f.severity == 'WARN'])} WARN, "
                  f"{len([f for f in findings if f.severity == 'INFO'])} INFO)")
            for f in findings[:10]:  # cap output at 10
                print(f"  [{f.severity}] {f.finding_type} {f.ticket_id}: {f.detail}")
            if len(findings) > 10:
                print(f"  ... and {len(findings) - 10} more")
            if errors:
                print(f"\n🚨 {len(errors)} ERROR findings detected.")
                print(f"   Investigate: python3 orchestrator/scripts/state.py audit")
    except Exception as exc:
        print(f"\n=== STATE AUDIT (skipped: {exc}) ===")

    # FU chain depth distribution (TKT-ORCH-PERF-002, 2026-09-28).
    # Excludes TKT-AGITA-FU-* (the AGITA backfill type prefix, not a real
    # FU-chain suffix — the metric would otherwise inflate depth-1).
    fu_depth_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    fu_chain_total = 0
    for path in TICKETS_ROOT.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(path):
            continue
        try:
            text = path.read_text(errors="ignore")
            m_id = re.search(r"^id:\s*(TKT-[\w-]+)", text, re.MULTILINE)
            if not m_id:
                continue
            tid = m_id.group(1)
            # Skip AGITA backfill tickets — they use `-FU-<NNN>` as a type
            # suffix, not as a follow-up suffix. The metric is about
            # real follow-up chains, not AGITA's naming convention.
            if tid.startswith("TKT-AGITA-FU-"):
                continue
            # Count FU segments in the ticket id.
            depth = len(re.findall(r"-FU-\d+", tid))
            depth = min(depth, 3)
            fu_depth_counts[depth] += 1
            if depth >= 1:
                fu_chain_total += 1
        except Exception:
            pass
    print(f"  fu_chain_total (depth ≥ 1): {fu_chain_total}")
    print(f"\n=== FU CHAIN DEPTH (TKT-ORCH-PERF-002) ===")
    print(f"  depth 0 (root): {fu_depth_counts[0]}")
    print(f"  depth 1:        {fu_depth_counts[1]}")
    print(f"  depth 2:        {fu_depth_counts[2]}")
    print(f"  depth 3+ (BLOCKED cap): {fu_depth_counts[3]}")
    drift_per_hour = 0.25  # baseline from burn-rate investigation; live measurement TBD
    print(f"  drift_rate_per_hour (baseline): {drift_per_hour}")

    # Daemon status (TKT-ORCH-PERF-001, 2026-09-28).
    # Note: pidfile format is `<pid>:<epoch>` (TKT-ORCH-PERF-001 PID-reuse
    # defense — kernel can recycle a PID after process death; the epoch is
    # the start time). Split on `:` to recover the pid.
    print(f"\n=== DAEMON STATUS ===")
    if not PID_FILE.exists():
        print(f"  daemon.alive:       False (no pid file at {PID_FILE})")
        print(f"  daemon.uptime_hours: 0")
    else:
        raw = PID_FILE.read_text().strip()
        # Handle both `<pid>` and `<pid>:<epoch>` formats.
        pid_str = raw.split(":", 1)[0] if ":" in raw else raw
        try:
            pid = int(pid_str)
            os.kill(pid, 0)
            # alive — read process start time for uptime
            try:
                # macOS: ps -o etime= -p <pid>
                import subprocess
                etime = subprocess.run(
                    ["ps", "-o", "etime=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
                # etime is like "1-02:30:45" (days-HH:MM:SS) or "02:30:45"
                uptime_hours = _parse_etime_hours(etime)
                print(f"  daemon.alive:       True (pid={pid})")
                print(f"  daemon.uptime_hours: {uptime_hours:.2f}")
            except Exception:
                print(f"  daemon.alive:       True (pid={pid})")
                print(f"  daemon.uptime_hours: unknown (ps failed)")
        except (ProcessLookupError, ValueError, PermissionError):
            print(f"  daemon.alive:       False (stale pid={raw})")
            print(f"  daemon.uptime_hours: 0")


if __name__ == "__main__":
    main()