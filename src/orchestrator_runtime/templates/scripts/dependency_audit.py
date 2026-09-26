"""Ticket dependency audit — first-class depends_on enforcement.

Per TKT-NNN (2026-09-19): for every ticket whose frontmatter declares
``status: QUEUED``, verify that the ticket either

  (a) carries an explicit ``depends_on:`` frontmatter field listing ticket
      IDs, OR
  (b) is in one of the first two phases (``P0`` or ``P1``), which are exempt
      because by construction every upstream predecessor is already landed
      before any ``P0`` / ``P1`` ticket is filed.

Tickets that satisfy neither (a) nor (b) are reported BLOCKED with the
remediation hint. This closes the ``burn-queue §2`` silent-fallback gap that
let cross-phase dependencies (e.g. TKT-NNN the project's primary backend depends on TKT-NNN
Keycloak) ride the naive phase-ordering heuristic without explicit declaration.

CLI usage:

    # Audit a single ticket.
    python3 orchestrator/scripts/dependency_audit.py TKT-NNN
        -> "OK: TKT-NNN dependencies satisfied" (exit 0)
        -> "BLOCKED: TKT-NNN missing depends_on declaration" (exit 1)

    # Audit every QUEUED ticket in the tree.
    python3 orchestrator/scripts/dependency_audit.py
        -> multi-line summary; exit 0 if all OK, exit 1 if any BLOCKED.

    # Ticket-id lookup is by exact ``id:`` frontmatter match; non-QUEUED
    # tickets trivially pass with status "OK: <id> not QUEUED".

Import-safe: this module performs no I/O and writes nothing on import. All
filesystem side effects are confined to ``argparse``-driven reads.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Phases that are exempt from the ``depends_on:`` requirement. By
#: construction every upstream predecessor is already landed before any
#: ``P0`` / ``P1`` ticket is filed, so an explicit ``depends_on:`` list
#: carries no useful signal for those phases.
EXEMPT_PHASES: frozenset[str] = frozenset({"P0", "P1"})

#: Frontmatter block delimiter pattern. Anchored on the file's first line so
#: a malformed ticket (no frontmatter) is silently skipped during a global
#: sweep rather than throwing — the global sweep treats missing/unparseable
#: frontmatter as "not QUEUED, trivially OK".
_FRONTMATTER_RE: re.Pattern[str] = re.compile(
    r"\A---\s*\n(.*?)(?:\n---\s*(?:\n|\Z)|\Z)",
    re.DOTALL,
)

#: Repo root resolved at import time (no side effects). ``dependency_audit.py``
#: lives at ``<repo>/orchestrator/scripts/`` so three ``.parent`` hops land at
#: the repo root — same pattern as ``checkpoint.py``.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_TICKETS_DIR: Path = _REPO_ROOT / "orchestrator" / "tickets"

#: Exit code semantics for shell callers.
_EXIT_OK: int = 0
_EXIT_BLOCKED: int = 1
_EXIT_ERROR: int = 2


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TicketFields:
    """Minimal frontmatter view needed by the audit — only the fields we read.

    Carries the source path for diagnostics; deliberately does NOT model every
    frontmatter field (this script is not a ticket-schema validator — that
    is a different ticket).
    """

    path: Path
    id: str
    status: str
    phase: str
    depends_on: str  # raw frontmatter value, e.g. "[TKT-FOO, TKT-BAR]"

    @property
    def is_queued(self) -> bool:
        return self.status == "QUEUED"

    @property
    def has_depends_on(self) -> bool:
        # The field is present if the raw value is non-empty AND not the
        # YAML ``null`` keyword. An explicit empty inline list ``[]`` is
        # treated as PRESENT (a conscious declaration of "no upstream
        # deps") per ``CONVENTIONS.md §Dependency declaration``. Missing
        # field, ``null``, and whitespace-only are treated as absent.
        stripped = self.depends_on.strip()
        if not stripped:
            return False
        if stripped.lower() == "null":
            return False
        return True

    @property
    def is_phase_exempt(self) -> bool:
        return self.phase in EXEMPT_PHASES


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter_block(block: str) -> dict[str, str]:
    """Naive YAML frontmatter reader — key: value lines only.

    This script does not need a full YAML parser. The depends_on field is
    inline-YAML (e.g. ``[TKT-FOO, TKT-BAR]``) which we capture verbatim.
    Multi-line string fields (``acceptance_decision: | ...``) are tolerated
    by stopping at the first newline after the key.
    """
    fields: dict[str, str] = {}
    for line in block.splitlines():
        # Skip comments and blank lines (matches standard YAML behaviour).
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # ``key: value`` — partition at the first colon only.
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def parse_ticket(path: Path) -> TicketFields | None:
    """Parse a ticket file's frontmatter into a ``TicketFields``.

    Returns ``None`` when the file lacks frontmatter, lacks an ``id``, or
    lacks the audit-relevant fields. The caller treats ``None`` as
    "trivially OK" for the global sweep.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    m = _FRONTMATTER_RE.search(text)
    if not m:
        return None

    fields = _parse_frontmatter_block(m.group(1))
    tid = fields.get("id")
    if not tid:
        return None

    return TicketFields(
        path=path,
        id=tid,
        status=fields.get("status", ""),
        phase=fields.get("phase", ""),
        depends_on=fields.get("depends_on", ""),
    )


def iter_ticket_paths(root: Path) -> Iterable[Path]:
    """Yield every ticket file under ``root``.

    Convention from ``CONVENTIONS.md §File naming``: ``TKT-<PHASE>-<NNN>-<slug>.md``.
    Excludes ``README.md`` files (directory-level docs, not tickets).
    """
    for path in sorted(root.rglob("TKT-*.md")):
        if path.name == "README.md":
            continue
        yield path


def discover_tickets(root: Path = _TICKETS_DIR) -> list[TicketFields]:
    """Walk ``root`` and return every parseable ticket."""
    out: list[TicketFields] = []
    for path in iter_ticket_paths(root):
        fields = parse_ticket(path)
        if fields is not None:
            out.append(fields)
    return out


# ---------------------------------------------------------------------------
# Audit core
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditResult:
    """One ticket's audit verdict."""

    ticket_id: str
    verdict: str  # "OK" | "BLOCKED"
    reason: str   # human-readable explanation

    @property
    def is_ok(self) -> bool:
        return self.verdict == "OK"


def audit_ticket(target_id: str, root: Path = _TICKETS_DIR) -> AuditResult:
    """Audit a single ticket by ``id:`` frontmatter match.

    Returns the audit verdict. Tickets whose status is anything other than
    ``QUEUED`` trivially pass (the dependency audit is a pre-claim gate per
    ``WORKFLOW.md §Step 2``, not a lifecycle tracker).
    """
    for fields in discover_tickets(root):
        if fields.id != target_id:
            continue
        if not fields.is_queued:
            return AuditResult(
                ticket_id=target_id,
                verdict="OK",
                reason=f"{target_id} not QUEUED (status={fields.status!r}); "
                       f"dependency audit is a pre-claim gate only",
            )
        if fields.has_depends_on:
            return AuditResult(
                ticket_id=target_id,
                verdict="OK",
                reason=f"depends_on present ({fields.depends_on})",
            )
        if fields.is_phase_exempt:
            return AuditResult(
                ticket_id=target_id,
                verdict="OK",
                reason=f"phase {fields.phase!r} is in EXEMPT_PHASES={set(EXEMPT_PHASES)}; "
                       f"explicit depends_on not required",
            )
        return AuditResult(
            ticket_id=target_id,
            verdict="BLOCKED",
            reason=f"phase={fields.phase!r} not exempt and no depends_on frontmatter",
        )

    # Not found — explicit error so callers can distinguish "no such ticket"
    # from "ticket exists but blocked".
    return AuditResult(
        ticket_id=target_id,
        verdict="ERROR",
        reason=f"ticket {target_id!r} not found under {root}",
    )


def audit_all(root: Path = _TICKETS_DIR) -> list[AuditResult]:
    """Audit every QUEUED ticket under ``root``."""
    results: list[AuditResult] = []
    for fields in discover_tickets(root):
        if not fields.is_queued:
            continue
        results.append(audit_ticket(fields.id, root))
    # Stable order for deterministic shell output (sorted by ticket id).
    results.sort(key=lambda r: r.ticket_id)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_single(result: AuditResult) -> str:
    """Format the single-ticket CLI output exactly per the AC spec."""
    if result.verdict == "OK":
        return f"OK: {result.ticket_id} dependencies satisfied"
    if result.verdict == "BLOCKED":
        return f"BLOCKED: {result.ticket_id} missing depends_on declaration"
    return f"ERROR: {result.reason}"


def _format_summary(results: list[AuditResult]) -> str:
    """Format the global sweep output as a human-readable multi-line block."""
    ok = [r for r in results if r.is_ok]
    blocked = [r for r in results if not r.is_ok]
    lines: list[str] = [
        "=== dependency_audit.py — TKT-NNN ===",
        f"Scanned QUEUED tickets: {len(results)}",
        f"  OK:      {len(ok)}",
        f"  BLOCKED: {len(blocked)}",
    ]
    if blocked:
        lines.append("")
        lines.append("BLOCKED (missing depends_on declaration):")
        for r in blocked:
            lines.append(f"  {r.ticket_id}  — {r.reason}")
        lines.append("")
        lines.append("Remediation: add `depends_on: [TKT-XXX-NNN, ...]` to the")
        lines.append("ticket frontmatter (see CONVENTIONS.md §Frontmatter), or")
        lines.append("move the ticket to phase P0/P1 if it has no upstream deps.")
    else:
        lines.append("")
        lines.append("All QUEUED tickets satisfy the dependency-declaration rule.")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser — kept separate so it's testable."""
    parser = argparse.ArgumentParser(
        prog="dependency_audit.py",
        description=(
            "Audit ticket frontmatter for the depends_on: declaration. "
            "Per TKT-NNN, every QUEUED ticket must either declare "
            "depends_on: or be in phase P0/P1."
        ),
    )
    parser.add_argument(
        "ticket_id",
        nargs="?",
        default=None,
        help=(
            "Specific ticket ID to audit (e.g. TKT-NNN). If omitted, "
            "every QUEUED ticket under orchestrator/tickets/ is scanned."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_TICKETS_DIR,
        help=(
            f"Tickets root directory (default: {_TICKETS_DIR}). Useful for "
            f"pointing at a fixture directory during testing."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns the process exit code."""
    args = _build_parser().parse_args(argv)

    if args.ticket_id is not None:
        result = audit_ticket(args.ticket_id, root=args.root)
        print(_format_single(result))
        if result.verdict == "OK":
            return _EXIT_OK
        if result.verdict == "BLOCKED":
            return _EXIT_BLOCKED
        return _EXIT_ERROR

    results = audit_all(root=args.root)
    print(_format_summary(results))
    if any(not r.is_ok for r in results):
        return _EXIT_BLOCKED
    return _EXIT_OK


__all__ = [
    "EXEMPT_PHASES",
    "AuditResult",
    "TicketFields",
    "audit_all",
    "audit_ticket",
    "discover_tickets",
    "main",
    "parse_ticket",
]


if __name__ == "__main__":
    sys.exit(main())
