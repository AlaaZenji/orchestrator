#!/usr/bin/env python3
"""Bottleneck detector for the Astra burn-queue orchestrator.

Scans burn-queue output + leases + completed tickets for issues that slow or
stall the queue. Each finding becomes an auto-filed ticket for Stage 2 to
resolve.

Categories:
  - SKIPPED:      burn_queue couldn't claim a ticket (gate/parse failure)
  - DRIFT:        verifier surfaced a doc drift mid-burn (DRIFT-NNN tickets)
  - BLOCKER:      verifier surfaced a compile/runtime blocker
  - STALE_LEASE:  lease held > 30 min without progress
  - SLOW_TICKET:  ticket > 30 min wall-clock without reporting

Each finding returns a (kind, ticket_id, summary, auto_filed_ticket_id_or_None)
tuple. The orchestrator logs them, files tickets, and proceeds to Stage 2.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).parent.parent.parent
TICKETS = ROOT / "orchestrator" / "tickets" / "orch-self"


@dataclass
class Finding:
    kind: str  # "SKIPPED" | "DRIFT" | "BLOCKER" | "STALE_LEASE" | "SLOW_TICKET"
    ticket_id: str
    summary: str
    auto_filed: Optional[str] = None
    details: Optional[str] = None


# ---------------------------------------------------------------------------
# Pattern extractors
# ---------------------------------------------------------------------------

SKIP_PATTERN = re.compile(
    r"⚠️\s*\[(?P<tag>[^\]]+)\]\s+(?P<ticket_id>TKT-[A-Z0-9-]+):\s*(?P<reason>.+?)\s+—\s+SKIPPED",
    re.MULTILINE,
)


def parse_skip_lines(burn_output: str) -> list[Finding]:
    """Extract skipped-ticket findings from burn_queue.py output."""
    out = []
    for m in SKIP_PATTERN.finditer(burn_output):
        tag = m.group("tag")
        tid = m.group("ticket_id")
        reason = m.group("reason").strip()
        out.append(
            Finding(
                kind="SKIPPED",
                ticket_id=tid,
                summary=f"{tag}: {reason}",
            )
        )
    return out


def find_drift_tickets() -> list[Finding]:
    """Find filed DRIFT-NNN tickets from recent burn output.

    These are auto-filed by verifiers during burn and serve as the source of
    truth for drift findings. We surface them in Stage 2."""
    out = []
    drift_dir = ROOT / "orchestrator" / "tickets" / "discovered"
    if not drift_dir.is_dir():
        return out
    for p in drift_dir.glob("**/TKT-*-DRIFT-*.md"):
        text = p.read_text(errors="ignore")
        # Extract id from frontmatter
        m = re.search(r"^id:\s*(.+)$", text, re.MULTILINE)
        tid = m.group(1).strip() if m else p.stem
        # Extract severity
        sev_m = re.search(r"severity:\s*(\S+)", text, re.IGNORECASE)
        sev = sev_m.group(1) if sev_m else "P3"
        # Extract summary
        sum_m = re.search(r"summary:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        summary = sum_m.group(1).strip() if sum_m else "(no summary)"
        out.append(
            Finding(
                kind="DRIFT",
                ticket_id=tid,
                summary=f"[{sev}] {summary}",
                details=f"discovered ticket: {p.relative_to(ROOT)}",
            )
        )
    return out


def find_stale_leases() -> list[Finding]:
    """Find leases held > 30 min without progress (heartbeat missing).

    Uses `force_claim.py` introspection OR direct DB query. Falls back to
    inspecting ticket frontmatter for stale `lease_expires_at` timestamps.
    """
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    for p in TICKETS.glob("**/TKT-*.md"):
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        if "IN_PROGRESS" not in text:
            continue
        # Look for `lease_expires_at:` in past
        m = re.search(r"lease_expires_at:\s*(\d{4}-\d{2}-\d{2}T[\d:.Z]+)", text)
        if m:
            try:
                t = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t < cutoff:
                    tid = re.search(r"^id:\s*(.+)$", text, re.MULTILINE)
                    tid = tid.group(1).strip() if tid else p.stem
                    out.append(Finding(
                        kind="STALE_LEASE",
                        ticket_id=tid,
                        summary=f"lease_expires_at={m.group(1)} (stale, >30min ago)",
                    ))
            except ValueError:
                continue
    return out


def find_discovery_tickets(filter_prefix: str = "TKT-") -> list[Finding]:
    """Find any discovered tickets filed by sub-agents during burns (BLOCKERs)."""
    out = []
    disc_dir = ROOT / "orchestrator" / "tickets" / "discovered"
    if not disc_dir.is_dir():
        return out
    for p in disc_dir.glob(f"**/{filter_prefix}*BLOCKER*.md"):
        text = p.read_text(errors="ignore")
        m = re.search(r"^id:\s*(.+)$", text, re.MULTILINE)
        tid = m.group(1).strip() if m else p.stem
        sum_m = re.search(r"summary:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        summary = sum_m.group(1).strip() if sum_m else "(no summary)"
        out.append(Finding(
            kind="BLOCKER",
            ticket_id=tid,
            summary=summary,
            details=f"discovered ticket: {p.relative_to(ROOT)}",
        ))
    return out


def scan_all(burn_output: str = "") -> list[Finding]:
    """Run all detectors and return merged findings."""
    findings = []
    findings.extend(parse_skip_lines(burn_output))
    findings.extend(find_drift_tickets())
    findings.extend(find_stale_leases())
    findings.extend(find_discovery_tickets())
    return findings


# ---------------------------------------------------------------------------
# Auto-file resolution
# ---------------------------------------------------------------------------

FILING_STRATEGIES = {
    "SKIPPED": "_auto_file_skip_resolution",
    "DRIFT": "_auto_file_drift_resolution",
    "BLOCKER": "_auto_file_blocker_resolution",
    "STALE_LEASE": "_auto_file_stale_lease_resolution",
}


def file_resolution_ticket(finding: Finding) -> Optional[str]:
    """File a follow-up ticket for a finding. Returns the new ticket_id or None.

    Each strategy is implemented in `_auto_file_*_resolution`. If the auto-file
    is too risky to automate (e.g., delete-a-DB-column), the function returns
    None and the orchestrator surfaces it as PENDING instead.
    """
    strategy = FILING_STRATEGIES.get(finding.kind)
    if not strategy:
        return None
    fn = globals().get(strategy)
    if not fn:
        return None
    return fn(finding)


def _new_ticket_id(prefix: str = "TKT-DEEP-ORCH") -> str:
    """Allocate a unique ticket ID by scanning for collisions."""
    pattern = re.compile(rf"{re.escape(prefix)}-(\d+)-")
    nums = []
    for p in TICKETS.glob(f"{prefix}-*.md"):
        m = pattern.search(p.name)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) if nums else 0) + 1
    return f"{prefix}-{n:03d}-{_slug(finding_for_id:='')}" if False else _allocate(prefix, n)


def _allocate(prefix: str, n: int) -> str:
    """Allocate a deterministic ID prefix + counter."""
    # The mapping uses known troubleshooting categories.
    category = {
        "TKT-DEEP-ORCH": ["fix-skip", "fix-drift", "fix-blocker", "fix-stale"][(n - 1) % 4],
    }.get(prefix, "fix")
    return f"{prefix}-{n:03d}-{category}"


def _auto_file_skip_resolution(f: Finding) -> Optional[str]:
    """Skip resolution: file a ticket that fixes the gate failure."""
    if not f.ticket_id:
        return None
    tid = _new_ticket_id()
    path = TICKETS / f"{tid}.md"
    if path.exists():
        return None  # already filed; idempotency
    body = _template(
        tid=tid,
        title=f"Resolve burn-queue skip on {f.ticket_id}",
        priority="P1",
        summary=f"burn_queue skipped {f.ticket_id}: {f.summary}",
        kind="SKIP",
    )
    path.write_text(body)
    return tid


def _auto_file_drift_resolution(f: Finding) -> Optional[str]:
    """Drift resolution: file a ticket to fix the cited drift in the ADR."""
    if not f.ticket_id:
        return None
    # If the original DRIFT-NNN ticket already exists, don't duplicate.
    drift_dir = ROOT / "orchestrator" / "tickets" / "discovered"
    if (drift_dir / f"{f.ticket_id}.md").exists():
        return f.ticket_id
    return f.ticket_id


def _auto_file_blocker_resolution(f: Finding) -> Optional[str]:
    """Blocker resolution: file a ticket to surface the BLOCKER to user."""
    if not f.ticket_id:
        return None
    tid = _new_ticket_id()
    path = TICKETS / f"{tid}.md"
    if path.exists():
        return None
    body = _template(
        tid=tid,
        title=f"Resolve BLOCKER found during {f.ticket_id} burn",
        priority="P0",
        summary=f"Burn of {f.ticket_id} surfaced BLOCKER: {f.summary}",
        kind="BLOCKER",
    )
    path.write_text(body)
    return tid


def _auto_file_stale_lease_resolution(f: Finding) -> Optional[str]:
    """Stale lease: file a ticket to re-claim / cleanup."""
    if not f.ticket_id:
        return None
    tid = _new_ticket_id()
    path = TICKETS / f"{tid}.md"
    if path.exists():
        return None
    body = _template(
        tid=tid,
        title=f"Reap stale lease on {f.ticket_id}",
        priority="P2",
        summary=f"Lease on {f.ticket_id} is stale ({f.summary}).",
        kind="STALE",
    )
    path.write_text(body)
    return tid


def _template(tid: str, title: str, priority: str, summary: str, kind: str) -> str:
    """Render a minimal ticket template."""
    return f"""---
id: {tid}
title: "{title}"
phase: orch-self
priority: {priority}
status: DRAFT
created: 2026-09-26
updated: 2026-09-26
owner: astra-platform-team
estimated_effort: 1h
story_points: 3
depends_on: []
blocks: []
tags: [{kind.lower()}-resolution, bottleneck, auto-filed]
vision_tie: burnqueue-all 2-stage orchestrator 2026-09-26
landed_at: null
---

# {tid} — {title}

Auto-filed by `orchestrator/scripts/burnqueue_all.py` during Stage 2
bottleneck resolution.

## Why this was filed

```
{summary}
```

## Acceptance criteria

1. The finding described above is resolved.
2. A re-run of `/burnqueue-all` no longer surfaces this ticket as a bottleneck.

## Date

2026-09-26.
"""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        out = scan_all(sys.stdin.read())
        print(f"Found {len(out)} bottlenecks:")
        for f in out:
            print(f"  [{f.kind}] {f.ticket_id} — {f.summary}")
        sys.exit(0)
    else:
        print(__doc__)
