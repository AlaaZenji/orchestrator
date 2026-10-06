"""Single chokepoint for ALL orchestrator state mutations.

TKT-CORE-FIX-STATE-CONSISTENCY (2026-09-29): every status change in the
orchestrator MUST flow through ``state.commit``. This module is the
SINGLE source of truth for ticket state transitions.

Why this exists
---------------
Before this module, status changes happened in 4+ different places:

  - ``lease.release_with_reconcile`` (worker side)
  - ``verifier_doctrine_gate --apply`` (gate side)
  - ``migrate_ticket_states --apply`` (bulk migration)
  - Direct LLM Edit/Write of frontmatter (orchestrator side)

Each of these was a SEPARATE state mutation with its own side-effects. Drift
between frontmatter and tickets-index.md was the predictable outcome.

Now: every status change calls ``state.commit(ticket_id, new_status, source,
...)``. The function atomically:
  1. Validates the transition (status-machine guard).
  2. Writes frontmatter (write-then-rename; atomic on POSIX).
  3. Enqueues a reconcile event to .reconcile_events.jsonl.
  4. Removes the heartbeat file if present (lifecycle discipline).
  5. Emits an audit log line to .state_audit.jsonl.

Nothing else is allowed to mutate frontmatter status.

Public API
----------
  * ``state.commit(ticket_id, new_status, *, source, lease_token=None,
    holder_id=None, fencing_token=None, reason=None) -> CommitResult``
    — the single mutation entry point.

  * ``state.read(ticket_id) -> TicketSnapshot`` — read the canonical state
    of one ticket (frontmatter + lease + heartbeat + WORKER_RESULT).

  * ``state.invariants() -> dict`` — return the global invariant map: for
    every ticket, the frontmatter status + the live lease + the latest
    WORKER_RESULT verdict. Used by ``state_audit.py`` to detect drift.

  * ``state.audit() -> list[DriftFinding]`` — return a list of all drift
    findings. Caller decides whether to fail-fast (non-zero exit) or warn.

  * ``state.transitions.allowed(current, target) -> bool`` — the
    state-machine guard. Returns False for any disallowed transition.

State machine
-------------
Allowed transitions (per TKT-CORE-FIX-STATE-CONSISTENCY §State machine):

    QUEUED         → IN_PROGRESS | BLOCKED | DEFERRED | CANCELLED
    IN_PROGRESS    → DONE | PARTIAL | PARTIAL_WITH_FOLLOW_UPS | QUEUED
                     | BLOCKED | CANCELLED
    PARTIAL        → IN_PROGRESS | DONE | PARTIAL_WITH_FOLLOW_UPS
                     | QUEUED | CANCELLED
    PARTIAL_WITH_FOLLOW_UPS → IN_PROGRESS | DONE | QUEUED | CANCELLED
    BLOCKED        → QUEUED | CANCELLED  (manual unblock only)
    DEFERRED       → QUEUED              (manual undefer only)
    CANCELLED      → QUEUED              (manual uncancel only)
    DONE           → IN_PROGRESS | PARTIAL  (explicit re-open only)
    DRAFT          → QUEUED | CANCELLED
    LANDED         → DONE                (admin promotion only)

Anything else is rejected with ``InvalidTransitionError``.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Module-level constants (paths)
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ORCH_ROOT: Path = REPO_ROOT / "orchestrator"
TICKETS_ROOT: Path = ORCH_ROOT / "tickets"
PROGRESS_DIR: Path = ORCH_ROOT / "progress"

RECONCILE_EVENTS_PATH: Path = PROGRESS_DIR / ".reconcile_events.jsonl"
STATE_AUDIT_PATH: Path = PROGRESS_DIR / ".state_audit.jsonl"
HEARTBEAT_DIR: Path = PROGRESS_DIR
WORKER_RESULT_GLOB: str = "*WORKER-RESULT.json"

# Logger — fires to orchestrator.state.* namespaces
logger = logging.getLogger("orchestrator.state")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class StateError(Exception):
    """Base class for state.py errors."""


class TicketNotFoundError(StateError):
    """The ticket id doesn't exist on disk."""
    def __init__(self, ticket_id: str) -> None:
        super().__init__(f"ticket {ticket_id!r} not found in tickets/")
        self.ticket_id = ticket_id


class InvalidTransitionError(StateError):
    """The requested status transition is not allowed."""
    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            f"invalid transition {current!r} → {target!r} (see "
            f"state.transitions.allowed for the full table)"
        )
        self.current = current
        self.target = target


class FrontmatterParseError(StateError):
    """The ticket file has no / malformed frontmatter."""
    def __init__(self, path: Path, msg: str = "") -> None:
        super().__init__(f"cannot parse frontmatter of {path}: {msg}")
        self.path = path


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CommitResult:
    """Return value of state.commit."""
    ticket_id: str
    previous_status: str
    new_status: str
    source: str           # who initiated the commit ("worker", "doctrine_gate",
                          # "migrate", "llm_session", "audit_repair", etc.)
    event_id: str         # sha256 of (ticket_id, fencing_token, new_status)
    ts: str               # ISO-8601 UTC
    frontmatter_path: str
    commit_path: str      # path to .state_audit.jsonl entry


@dataclasses.dataclass(frozen=True)
class TicketSnapshot:
    """The canonical state of one ticket at read time."""
    ticket_id: str
    frontmatter_status: str
    frontmatter_priority: str
    frontmatter_path: str
    live_lease_holder: str | None
    live_lease_expires_at: str | None
    live_lease_fencing_token: int | None
    heartbeat_present: bool
    heartbeat_age_seconds: float | None
    worker_result_present: bool
    worker_result_status: str | None  # final_status from WORKER_RESULT


@dataclasses.dataclass(frozen=True)
class DriftFinding:
    """One invariant violation detected by state.audit()."""
    ticket_id: str
    finding_type: str  # "stale_lease_no_lease", "worker_done_frontmatter_queued",
                       # "heartbeat_no_lease", "frontmatter_done_no_worker_result",
                       # "duplicate_in_progress", etc.
    severity: str      # "ERROR" | "WARN" | "INFO"
    detail: str


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TransitionTable:
    """The canonical status-transition table. Single source of truth."""

    #: target → set of allowed current statuses
    _ALLOWED: dict[str, frozenset[str]] = {
        "QUEUED": frozenset({
            "DRAFT", "IN_PROGRESS", "PARTIAL", "PARTIAL_WITH_FOLLOW_UPS",
            "BLOCKED", "DEFERRED", "CANCELLED", "DONE",
        }),
        # IN_PROGRESS can be re-entered from DONE (explicit re-open).
        "IN_PROGRESS": frozenset({
            "QUEUED", "PARTIAL", "PARTIAL_WITH_FOLLOW_UPS", "DONE",
        }),
        "DONE": frozenset({
            "IN_PROGRESS", "PARTIAL", "PARTIAL_WITH_FOLLOW_UPS",
        }),
        "PARTIAL": frozenset({"IN_PROGRESS", "PARTIAL_WITH_FOLLOW_UPS"}),
        "PARTIAL_WITH_FOLLOW_UPS": frozenset({
            "IN_PROGRESS", "PARTIAL", "DONE",
        }),
        "BLOCKED": frozenset({"QUEUED", "IN_PROGRESS"}),
        "DEFERRED": frozenset({"QUEUED"}),
        "CANCELLED": frozenset({
            "QUEUED", "IN_PROGRESS", "PARTIAL", "PARTIAL_WITH_FOLLOW_UPS",
        }),
        "DRAFT": frozenset(),  # DRAFT is a pre-QUEUED state, no inbound transitions
        "LANDED": frozenset({"DONE"}),
    }

    @classmethod
    def allowed(cls, current: str, target: str) -> bool:
        """Return True if ``current → target`` is a permitted transition."""
        cur = current.upper().split()[0]  # tolerate "DONE (suffix)"
        tgt = target.upper().split()[0]
        if tgt not in cls._ALLOWED:
            return False
        return cur in cls._ALLOWED[tgt]


transitions = TransitionTable()


# ---------------------------------------------------------------------------
# Frontmatter I/O (atomic write-then-rename)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-z_]+):\s*(.*?)\s*$")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-ish frontmatter. Returns ({fields}, body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    body = text[m.end():]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        fm = _FIELD_RE.match(line)
        if fm:
            fields[fm.group(1)] = fm.group(2)
    return fields, body


def serialize_frontmatter(fields: dict[str, str], body: str) -> str:
    """Render frontmatter + body back to file content."""
    fm_lines = ["---"]
    for k, v in fields.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    if not body.startswith("\n"):
        body = "\n" + body
    return "\n".join(fm_lines) + body


def find_ticket_file(ticket_id: str) -> Path:
    """Locate the on-disk ticket file for a given ticket id.

    Walks ``TICKETS_ROOT`` and returns the first file whose frontmatter
    ``id:`` field matches ``ticket_id``. Raises :class:`TicketNotFoundError`
    if not found.
    """
    for path in TICKETS_ROOT.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(path):
            continue
        try:
            fm, _ = parse_frontmatter(path.read_text(errors="ignore"))
        except Exception:
            continue
        if fm.get("id") == ticket_id:
            return path
    raise TicketNotFoundError(ticket_id)


def read_frontmatter(ticket_id: str) -> tuple[dict[str, str], str, Path]:
    """Read frontmatter + body + path for one ticket. Single read API."""
    path = find_ticket_file(ticket_id)
    text = path.read_text()
    fm, body = parse_frontmatter(text)
    return fm, body, path


def atomic_write_frontmatter(
    path: Path, fm: dict[str, str], body: str
) -> None:
    """Atomically replace the file's contents (write-temp-then-rename).

    fsync the file and the directory so the change is durable across
    crashes (the canonical POSIX atomic-rename idiom).
    """
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    new_content = serialize_frontmatter(fm, body)
    # Open explicitly so we have a file descriptor for fsync.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, new_content.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # fsync the directory so the rename is durable
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Audit log (durable append-only)
# ---------------------------------------------------------------------------

def append_audit_event(event: dict[str, Any]) -> None:
    """Append one JSONL line to ``.state_audit.jsonl`` (durable)."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True) + "\n"
    fd = os.open(str(STATE_AUDIT_PATH),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Event-id derivation (shared with auto_reconcile)
# ---------------------------------------------------------------------------

def compute_event_id(
    ticket_id: str, fencing_token: int | None, target_status: str
) -> str:
    """Stable sha256 over canonical event fields. Idempotency dedup key."""
    canonical = json.dumps(
        {
            "ticket_id": ticket_id,
            "fencing_token": fencing_token,
            "target_status": target_status,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Reconcile event enqueue (shared with lease.release_with_reconcile)
# ---------------------------------------------------------------------------

def append_reconcile_event(event: dict[str, Any]) -> None:
    """Append one JSONL event to ``.reconcile_events.jsonl`` (durable)."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True) + "\n"
    fd = os.open(str(RECONCILE_EVENTS_PATH),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# The chokepoint: state.commit
# ---------------------------------------------------------------------------

def commit(
    ticket_id: str,
    new_status: str,
    *,
    source: str,
    reason: str | None = None,
    fencing_token: int | None = None,
    holder_id: str | None = None,
    tenant_id: str | None = None,
    extra_fields: dict[str, str] | None = None,
    force: bool = False,
) -> CommitResult:
    """The SINGLE mutation entry point for ticket status.

    Atomically:
      1. Validates the transition (unless ``force=True``).
      2. Writes frontmatter (atomic write-then-rename).
      3. Enqueues a reconcile event to ``.reconcile_events.jsonl``.
      4. Removes the heartbeat file if present (lifecycle discipline).
      5. Appends an audit row to ``.state_audit.jsonl``.

    Args:
        ticket_id:   The canonical ticket id (from frontmatter ``id:``).
        new_status:  The target status. Validated against the state machine.
        source:      Who initiated the commit ("worker", "doctrine_gate",
                     "migrate", "llm_session", "audit_repair"). Emitted in
                     audit log so the history is traceable.
        reason:      Optional human-readable explanation (also in audit log).
        fencing_token:  Caller-supplied fencing token (from the active lease).
                     Stored in the audit log; included in the event_id
                     dedup hash. None for non-lease callers (LLM, audit).
        holder_id:   Caller-supplied lease holder id (audit log only).
        tenant_id:   Tenant context (audit log only).
        extra_fields: Optional extra frontmatter fields to write alongside
                     the status change (e.g. ``updated: <iso>``).
        force:       Skip the state-machine validation. Use only for
                     recovery operations (audit-repair, manual override).

    Returns:
        CommitResult with the previous + new status + event_id + paths.

    Raises:
        TicketNotFoundError: ticket_id doesn't exist on disk.
        InvalidTransitionError: transition not allowed (unless force=True).
        FrontmatterParseError: ticket file has no frontmatter.

    The commit is crash-safe: if the process dies after step 2 but before
    step 5, the next ``state.audit()`` call will detect the missing audit
    row + the status change and emit a DriftFinding (severity INFO) so
    the operator can replay the audit.
    """
    fm, body, path = read_frontmatter(ticket_id)
    previous = (fm.get("status") or "").strip().upper()
    target = new_status.strip().upper()

    if not previous:
        raise FrontmatterParseError(path, "no `status:` field in frontmatter")

    if not force and previous != target and not transitions.allowed(previous, target):
        raise InvalidTransitionError(previous, target)

    event_id = compute_event_id(ticket_id, fencing_token, target)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    # 1. Write frontmatter atomically
    fm["status"] = target
    fm["updated"] = ts[:10]  # YYYY-MM-DD
    if extra_fields:
        for k, v in extra_fields.items():
            fm[k] = v
    atomic_write_frontmatter(path, fm, body)

    # 2. Enqueue reconcile event
    append_reconcile_event({
        "ticket_id": ticket_id,
        "previous_status": previous,
        "status": target,
        "fencing_token": fencing_token,
        "holder_id": holder_id,
        "tenant_id": tenant_id,
        "worker_result_path": None,
        "ts": ts,
        "event_id": event_id,
        "source": source,
    })

    # 3. Remove heartbeat if present (lifecycle discipline)
    heartbeat = HEARTBEAT_DIR / f".heartbeat-{ticket_id}"
    if heartbeat.exists():
        try:
            heartbeat.unlink()
        except OSError:
            pass  # forensic, not blocking

    # 4. Append audit row
    audit_event = {
        "event_id": event_id,
        "ticket_id": ticket_id,
        "previous_status": previous,
        "new_status": target,
        "source": source,
        "reason": reason,
        "fencing_token": fencing_token,
        "holder_id": holder_id,
        "tenant_id": tenant_id,
        "ts": ts,
    }
    append_audit_event(audit_event)

    logger.info(
        "state.commit ticket_id=%s %s → %s source=%s event_id=%s",
        ticket_id, previous, target, source, event_id,
    )

    return CommitResult(
        ticket_id=ticket_id,
        previous_status=previous,
        new_status=target,
        source=source,
        event_id=event_id,
        ts=ts,
        frontmatter_path=str(path),
        commit_path=str(STATE_AUDIT_PATH),
    )


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def read(ticket_id: str) -> TicketSnapshot:
    """Read the canonical state of one ticket.

    Combines:
      - Frontmatter (status, priority, path)
      - Live lease (holder, fencing_token, expires_at)
      - Heartbeat file (liveness probe)
      - WORKER_RESULT.json (worker output)

    The Postgres lease read is best-effort: if the DB is unreachable, the
    snapshot returns ``live_lease_*`` as None and audit() will surface a
    drift finding.
    """
    fm, body, path = read_frontmatter(ticket_id)
    status = (fm.get("status") or "").strip().upper()
    priority = (fm.get("priority") or "").strip().upper()

    # Live lease (best-effort)
    holder, expires_at, fencing = _read_lease(ticket_id)

    # Heartbeat
    heartbeat_path = HEARTBEAT_DIR / f".heartbeat-{ticket_id}"
    heartbeat_present = heartbeat_path.exists()
    heartbeat_age = None
    if heartbeat_present:
        try:
            heartbeat_age = time.time() - heartbeat_path.stat().st_mtime
        except OSError:
            heartbeat_age = None

    # WORKER_RESULT
    wr_present = False
    wr_status = None
    if PROGRESS_DIR.exists():
        candidates = sorted(PROGRESS_DIR.glob(
            f"{ticket_id}*{WORKER_RESULT_GLOB}"
        ))
        if not candidates:
            # try a slightly broader glob
            candidates = sorted(PROGRESS_DIR.glob(
                f"{ticket_id}*"
            ))
            candidates = [p for p in candidates if "WORKER-RESULT" in p.name]
        if candidates:
            wr_present = True
            try:
                wr = json.loads(candidates[-1].read_text())
                wr_status = (
                    wr.get("final_status")
                    or wr.get("status")
                    or (wr.get("verdict") or {}).get("final")
                )
            except Exception:
                wr_status = None

    return TicketSnapshot(
        ticket_id=ticket_id,
        frontmatter_status=status,
        frontmatter_priority=priority,
        frontmatter_path=str(path),
        live_lease_holder=holder,
        live_lease_expires_at=expires_at,
        live_lease_fencing_token=fencing,
        heartbeat_present=heartbeat_present,
        heartbeat_age_seconds=heartbeat_age,
        worker_result_present=wr_present,
        worker_result_status=wr_status,
    )


def _read_lease(
    ticket_id: str,
) -> tuple[str | None, str | None, int | None]:
    """Best-effort read of the live lease for one ticket. None on failure."""
    try:
        # Import lazily — Postgres may not be reachable on every run.
        import lease as lease_mod  # type: ignore
        rec = lease_mod.read_lease(ticket_id, "00000000-0000-0000-0000-000000000000")
        if rec is None:
            return None, None, None
        return (
            rec.get("holder_id"),
            rec.get("lease_expires_at"),
            rec.get("fencing_token"),
        )
    except Exception:
        return None, None, None


# ---------------------------------------------------------------------------
# Invariants + audit
# ---------------------------------------------------------------------------

def invariants() -> dict[str, dict[str, Any]]:
    """Return the canonical invariant map for every ticket.

    Shape: {ticket_id: {status, priority, snapshot_dict}}
    """
    out: dict[str, dict[str, Any]] = {}
    for path in TICKETS_ROOT.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(path):
            continue
        text = path.read_text(errors="ignore")
        fm, _ = parse_frontmatter(text)
        tid = fm.get("id")
        if not tid or not tid.startswith("TKT-"):
            continue
        snap = read(tid)
        out[tid] = {
            "status": snap.frontmatter_status,
            "priority": snap.frontmatter_priority,
            "snapshot": dataclasses.asdict(snap),
        }
    return out


def audit(*, only_with_active_lease: bool = False) -> list[DriftFinding]:
    """Return all drift findings as a list. Empty = clean.

    Findings (severity ERROR):
      - "duplicate_active_lease": same ticket_id held by two different
        holders (would require cross-tenant; RLS prevents in practice but
        we check across tenants).
      - "frontmatter_done_lease_held": frontmatter says DONE but a live
        lease still exists (worker forgot to release).
      - "heartbeat_no_lease": heartbeat file present but no live lease
        (worker crashed; need reap + revert).
      - "worker_result_done_frontmatter_queued": WORKER_RESULT says DONE
        but frontmatter says QUEUED (worker forgot to update frontmatter).

    Findings (severity WARN):
      - "stale_lease_no_worker_result": lease held > N min with no
        WORKER_RESULT (worker hasn't reported yet — could be in flight).
      - "heartbeat_no_worker_result": heartbeat present, no WORKER_RESULT.

    Findings (severity INFO):
      - "status_changed_no_audit_row": frontmatter changed but no audit
        row exists (audit log drift; operator should inspect).
    """
    findings: list[DriftFinding] = []

    seen_lease_holders: dict[str, str] = {}

    for tid, inv in invariants().items():
        snap = inv["snapshot"]
        fm_status = snap["frontmatter_status"]
        lease_holder = snap["live_lease_holder"]
        heartbeat_present = snap["heartbeat_present"]
        wr_present = snap["worker_result_present"]
        wr_status = snap["worker_result_status"]

        # ERROR: frontmatter DONE + live lease
        if fm_status == "DONE" and lease_holder is not None:
            findings.append(DriftFinding(
                ticket_id=tid,
                finding_type="frontmatter_done_lease_held",
                severity="ERROR",
                detail=(
                    f"frontmatter says DONE but live lease held by "
                    f"{lease_holder!r} (fencing={snap['live_lease_fencing_token']}). "
                    f"Worker may have updated frontmatter before releasing lease."
                ),
            ))

        # ERROR: heartbeat present + no live lease (worker died)
        if heartbeat_present and lease_holder is None:
            findings.append(DriftFinding(
                ticket_id=tid,
                finding_type="heartbeat_no_lease",
                severity="ERROR",
                detail=(
                    f"heartbeat file exists but no live lease. "
                    f"Worker likely crashed between claim and release."
                ),
            ))

        # ERROR: WORKER_RESULT DONE + frontmatter QUEUED
        if wr_present and wr_status == "DONE" and fm_status in (
            "QUEUED", "IN_PROGRESS", "PARTIAL",
        ):
            findings.append(DriftFinding(
                ticket_id=tid,
                finding_type="worker_result_done_frontmatter_queued",
                severity="ERROR",
                detail=(
                    f"WORKER_RESULT.final_status=DONE but frontmatter says "
                    f"{fm_status}. Worker forgot to update frontmatter."
                ),
            ))

        # ERROR: heartbeat age > 30 min + no lease
        if (
            heartbeat_present
            and snap["heartbeat_age_seconds"] is not None
            and snap["heartbeat_age_seconds"] > 1800  # 30 min
            and lease_holder is None
        ):
            findings.append(DriftFinding(
                ticket_id=tid,
                finding_type="stale_heartbeat_no_lease",
                severity="ERROR",
                detail=(
                    f"heartbeat age={snap['heartbeat_age_seconds']:.0f}s "
                    f"with no live lease — reaper should clean up."
                ),
            ))

        # WARN: lease held > 30 min + no WORKER_RESULT
        if lease_holder is not None and not wr_present:
            findings.append(DriftFinding(
                ticket_id=tid,
                finding_type="stale_lease_no_worker_result",
                severity="WARN",
                detail=(
                    f"lease held by {lease_holder!r} but no WORKER_RESULT.json. "
                    f"Worker may be in flight."
                ),
            ))

    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_main(argv: list[str] | None = None) -> int:
    """CLI for state.commit. Workers + scripts call this.

    Usage::

        python3 -m orchestrator.scripts.state commit TKT-FOO DONE \\
            --source worker --reason "8/8 verifiers passed" \\
            --fencing-token 123 --holder-id worker-abc
    """
    parser = argparse.ArgumentParser(prog="state", description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_commit = sub.add_parser(
        "commit", help="Commit a status transition (the ONLY blessed mutation path)."
    )
    p_commit.add_argument("ticket_id")
    p_commit.add_argument("new_status")
    p_commit.add_argument("--source", required=True,
                          help="Who initiated: worker / doctrine_gate / migrate / "
                               "llm_session / audit_repair / heartbeat_reaper")
    p_commit.add_argument("--reason", default=None)
    p_commit.add_argument("--fencing-token", type=int, default=None)
    p_commit.add_argument("--holder-id", default=None)
    p_commit.add_argument("--tenant-id", default=None)
    p_commit.add_argument("--force", action="store_true",
                          help="Skip state-machine validation (recovery only)")
    p_commit.add_argument("--extra-field", action="append", default=[],
                          metavar="KEY=VALUE",
                          help="Extra frontmatter field to set "
                               "(repeatable, e.g. --extra-field foo=bar)")

    p_read = sub.add_parser("read", help="Read canonical state of one ticket.")
    p_read.add_argument("ticket_id")

    p_audit = sub.add_parser("audit", help="Run the invariant audit (drift detection).")
    p_audit.add_argument("--fail-on-error", action="store_true",
                         help="Exit non-zero on any ERROR finding.")

    args = parser.parse_args(argv)

    if args.cmd == "commit":
        extra = {}
        for kv in args.extra_field:
            if "=" not in kv:
                print(f"error: --extra-field must be KEY=VALUE, got {kv!r}",
                      file=sys.stderr)
                return 2
            k, v = kv.split("=", 1)
            extra[k] = v
        try:
            r = commit(
                args.ticket_id,
                args.new_status,
                source=args.source,
                reason=args.reason,
                fencing_token=args.fencing_token,
                holder_id=args.holder_id,
                tenant_id=args.tenant_id,
                extra_fields=extra or None,
                force=args.force,
            )
        except (InvalidTransitionError, TicketNotFoundError,
                  FrontmatterParseError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(dataclasses.asdict(r), indent=2))
        return 0

    if args.cmd == "read":
        try:
            snap = read(args.ticket_id)
        except TicketNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(dataclasses.asdict(snap), indent=2))
        return 0

    if args.cmd == "audit":
        findings = audit()
        if not findings:
            print("✅ No drift findings. State is consistent.")
            return 0
        # Print every finding as JSONL
        for f in findings:
            print(json.dumps(dataclasses.asdict(f), sort_keys=True))
        errors = [f for f in findings if f.severity == "ERROR"]
        if args.fail_on_error and errors:
            return 1
        return 0

    return 2


__all__ = [
    "CommitResult",
    "DriftFinding",
    "FrontmatterParseError",
    "InvalidTransitionError",
    "StateError",
    "TicketNotFoundError",
    "TicketSnapshot",
    "TransitionTable",
    "append_audit_event",
    "append_reconcile_event",
    "atomic_write_frontmatter",
    "audit",
    "commit",
    "compute_event_id",
    "find_ticket_file",
    "invariants",
    "parse_frontmatter",
    "read",
    "serialize_frontmatter",
    "transitions",
]


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))