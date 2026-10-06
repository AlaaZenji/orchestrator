"""Ticket state migration — Orchestrator 2.0 Phase 4A.

**Purpose.** Per Phase 3B (state model extensions), the ticket state machine was
extended from 7 to 14 states (per Final Follow-Up Directive §5). This script
migrates existing tickets in `orchestrator/tickets/**/*.md` from the old schema
to the new one.

**Migration rules:**

  Old status         New status          Reason
  ──────────────────────────────────────────────────────────────────────
  IN_PROGRESS        EXECUTING           Per Phase 3B — IN_PROGRESS was renamed
                                        to EXECUTING (semantic: actively
                                        implementing; matches the new mid-flight
                                        state machine).
  DEFERRED           DEPRECATED          Per Phase 3B — DEFERRED meant "do not
                                        pick up"; DEPRECATED carries the same
                                        semantic in the new 14-state enum.
  DRAFT              DRAFT               No change (already in the new enum).
  All other statuses — no change.

**Dry-run by default.** The script reports what it WOULD change without
modifying any files. Pass `--apply` to actually write changes.

**Backup.** When `--apply` is set, every modified file is backed up to
`orchestrator/tickets/.migrate-backups/{timestamp}/<original-path>`. The
backup is created BEFORE the write so a crash mid-write can be recovered.

**Stdlib-only.**

**Usage.**

    # Dry-run (report only)
    python3 scripts/migrate_ticket_states.py

    # Actually apply
    python3 scripts/migrate_ticket_states.py --apply

    # Custom ticket root
    python3 scripts/migrate_ticket_states.py --tickets-root /path/to/tickets --apply

**Date.** 2026-09-20 (Phase 4A).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    from .vision_audit import REPO_ROOT
    from .ticket_state_machine import State
except ImportError:
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from vision_audit import REPO_ROOT
    from ticket_state_machine import State


# --- Migration rules ---------------------------------------------------------

# Map old -> new. Anything not listed here is left unchanged.
MIGRATION_MAP: dict[str, str] = {
    "IN_PROGRESS": State.EXECUTING.value,
    "DEFERRED": State.DEPRECATED.value,
}

# Validation: every status value MUST be in the 14-state enum.
VALID_STATES: set[str] = {s.value for s in State}


# --- Frontmatter parser (stdlib) --------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict[str, str], str, str]:
    """Parse YAML-ish frontmatter from a Markdown file.

    Returns: (frontmatter_dict, body_text, raw_frontmatter_block).
    The dict is a flat key-value mapping; multi-line values are not supported
    (sufficient for ticket frontmatter).
    """
    if not text.startswith("---"):
        return {}, text, ""
    # Find the closing '---'.
    m = re.search(r"^---\s*$", text[3:], re.MULTILINE)
    if m is None:
        # Tolerate missing closing '---' (some pre-existing tickets are malformed).
        # Treat the entire file as frontmatter until first blank line, then take body after.
        # Walk lines: collect YAML-shaped lines, stop at first non-YAML-shaped line.
        fields: dict[str, str] = {}
        collected_raw: list[str] = []
        body_start_idx: Optional[int] = None
        body_offset = 3
        for line in text[3:].splitlines(keepends=True):
            stripped = line.rstrip("\n")
            m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", stripped)
            if m2 and stripped.endswith(":") is False and not stripped.endswith(":"):
                # Continuation of YAML key-value
                key = m2.group(1)
                val = m2.group(2).strip()
                # Don't overwrite on duplicate keys (first wins)
                if key not in fields:
                    fields[key] = val
                    collected_raw.append(stripped)
            elif stripped == "":
                # Blank line — could be end of frontmatter
                # Continue scanning to see if more YAML keys follow
                collected_raw.append(stripped)
                continue
            elif stripped.startswith("#") or stripped.startswith(" ") or stripped.startswith("\t"):
                # Markdown body line
                body_start_idx = text[3:].index(line)
                break
            else:
                # Non-YAML-shaped line — treat as body start
                body_start_idx = text[3:].index(line)
                break
        if body_start_idx is None:
            body_start_idx = len(text[3:])
        body = text[3 + body_start_idx:].lstrip("\n")
        return fields, body, "\n".join(collected_raw)
    end = m.start() + 3
    raw = text[3:end]
    body = text[end + 3:].lstrip("\n")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if m2:
            fields[m2.group(1)] = m2.group(2).strip()
    return fields, body, raw


def _serialize_frontmatter(fields: dict[str, str], body: str, raw: str) -> str:
    """Re-serialize frontmatter (preserving order if possible) + body."""
    # Re-build preserving the original ordering.
    lines = []
    seen = set()
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            lines.append(line)
            continue
        m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
        if m2:
            k = m2.group(1)
            seen.add(k)
            # If we're changing this field's value, use the new value.
            if k in fields:
                v = fields[k]
                # Preserve comment suffix if present.
                rest = line[len(m2.group(0)):].lstrip()
                # Just rebuild simply: k: v
                lines.append(f"{k}: {v}")
            else:
                lines.append(line)
        else:
            lines.append(line)
    # Add any new fields that didn't appear in the raw.
    for k, v in fields.items():
        if k not in seen:
            lines.append(f"{k}: {v}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


# --- Result schema -----------------------------------------------------------

@dataclass
class FileReport:
    path: pathlib.Path
    old_status: Optional[str]
    new_status: Optional[str]
    action: str   # "migrate" | "valid-noop" | "invalid-status" | "no-frontmatter" | "read-error"
    note: str = ""


# --- Migration runner --------------------------------------------------------

def _walk_tickets(tickets_root: pathlib.Path) -> list[pathlib.Path]:
    """Return all .md files under tickets_root, excluding README.md and the
    `.migrate-backups/` directory (which contains pre-migration snapshots)."""
    out: list[pathlib.Path] = []
    for p in tickets_root.rglob("*.md"):
        if p.name == "README.md":
            continue
        # Skip the backups directory (contains pre-migration snapshots with
        # legacy statuses; the migration would re-migrate them otherwise).
        try:
            rel = p.relative_to(tickets_root)
        except ValueError:
            rel = p
        if rel.parts and rel.parts[0].startswith(".migrate-backups"):
            continue
        out.append(p)
    return sorted(out)


def migrate_file(path: pathlib.Path, apply: bool,
                 backup_dir: Optional[pathlib.Path]) -> FileReport:
    """Migrate one ticket file. Returns a report describing the action.

    TKT-CORE-FIX-STATE-CONSISTENCY (2026-09-29): when applying, this
    delegates the status write to ``state.commit()`` so the reconcile
    queue + audit log are kept in sync with the frontmatter change.
    """
    try:
        text = path.read_text()
    except Exception as e:
        return FileReport(path, None, None, "read-error", str(e))

    fields, body, raw = _parse_frontmatter(text)
    if not fields and not raw:
        return FileReport(path, None, None, "no-frontmatter",
                          "no YAML frontmatter found")

    old_status = fields.get("status")
    if old_status is None:
        return FileReport(path, None, None, "no-frontmatter",
                          "no `status:` field in frontmatter")

    # Validate against the 14-state enum (post-migration).
    target_status = MIGRATION_MAP.get(old_status, old_status)
    if target_status not in VALID_STATES:
        return FileReport(path, old_status, None, "invalid-status",
                          f"target status {target_status!r} not in 14-state enum")

    if target_status == old_status:
        return FileReport(path, old_status, target_status, "valid-noop", "")

    if not apply:
        return FileReport(path, old_status, target_status, "migrate",
                          "dry-run; would change status")

    # Backup first.
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        rel = path.relative_to(path.parent.parent) if path.parent.parent in path.parents else path.name
        backup_path = backup_dir / rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)

    # TKT-CORE-FIX-STATE-CONSISTENCY: route the mutation through the
    # state.commit chokepoint so the reconcile queue + audit log stay
    # in sync. force=True because migration is an admin override
    # (cross-state-machine transitions may be needed for legacy states).
    ticket_id = fields.get("id")
    if ticket_id and ticket_id.startswith("TKT-"):
        try:
            import state as state_mod  # type: ignore
            state_mod.commit(
                ticket_id,
                target_status,
                source="migrate",
                reason=(
                    f"migrate_ticket_states: {old_status} → {target_status}"
                ),
                force=True,
            )
            return FileReport(path, old_status, target_status, "migrate",
                              f"applied via state.commit: {old_status} -> {target_status}")
        except Exception as exc:
            # Fall through to legacy write if state.commit fails.
            print(
                f"WARN: state.commit failed for {ticket_id}: {exc}; "
                f"falling back to legacy write (next reconcile --rebuild "
                f"will fix the index).",
                file=sys.stderr,
            )

    # Legacy write (fallback). Atomic write-temp-then-rename.
    fields["status"] = target_status
    new_text = _serialize_frontmatter(fields, body, raw)
    tmp = path.with_suffix(path.suffix + ".migrate-tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)
    return FileReport(path, old_status, target_status, "migrate",
                      f"applied (legacy): {old_status} -> {target_status}")


# --- CLI ---------------------------------------------------------------------

def _main() -> int:
    p = argparse.ArgumentParser(description="Migrate ticket statuses to the 14-state enum (Phase 3B).")
    p.add_argument("--tickets-root", default=str(REPO_ROOT / "orchestrator" / "tickets"),
                   help="Root of the ticket corpus")
    p.add_argument("--apply", action="store_true",
                   help="Apply changes (default: dry-run; reports only)")
    p.add_argument("--backup-dir", default=None,
                   help="Backup directory (default: <tickets-root>/.migrate-backups/<timestamp>)")
    args = p.parse_args()

    tickets_root = pathlib.Path(args.tickets_root)
    if not tickets_root.exists():
        print(f"ERROR: tickets-root does not exist: {tickets_root}")
        return 1

    backup_dir = pathlib.Path(args.backup_dir) if args.backup_dir else (
        tickets_root / ".migrate-backups" / dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%SZ")
    )

    paths = _walk_tickets(tickets_root)
    print(f"Scanning {len(paths)} ticket file(s) under {tickets_root}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    if args.apply:
        print(f"Backup directory: {backup_dir}")
    print()

    reports: list[FileReport] = []
    for p in paths:
        r = migrate_file(p, apply=args.apply, backup_dir=backup_dir if args.apply else None)
        reports.append(r)

    # Tally.
    by_action: dict[str, int] = {}
    for r in reports:
        by_action[r.action] = by_action.get(r.action, 0) + 1

    # Print summary by action.
    for action, count in sorted(by_action.items(), key=lambda x: -x[1]):
        print(f"  {action:18s}: {count}")

    # Print migration candidates in dry-run or applied migrations.
    migrate_reports = [r for r in reports if r.action == "migrate"]
    if migrate_reports:
        print()
        print(f"{'Would apply' if not args.apply else 'Applied'} changes:")
        for r in migrate_reports:
            print(f"  {r.old_status:14s} -> {r.new_status:14s}  {r.path.relative_to(tickets_root)}")

    invalid = [r for r in reports if r.action == "invalid-status"]
    if invalid:
        print()
        print(f"INVALID STATUSES (no migration rule applies):")
        for r in invalid:
            print(f"  {r.old_status!r:14s}  {r.path.relative_to(tickets_root)}  ({r.note})")

    # Exit code: 0 if all OK; 1 if any invalid status encountered (forces manual review).
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(_main())
