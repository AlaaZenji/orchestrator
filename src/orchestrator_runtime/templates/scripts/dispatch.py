#!/usr/bin/env python3
"""Worker auto-dispatch — Stage 3 of /burnqueue-all orchestrator.

The 2-stage orchestrator (burnqueue_all.py) currently CLAIMS tickets
via burn_queue.py but does NOT dispatch sub-agents to actually do the work.
This module bridges that gap by:

  1. Reading CLAIMED tickets from burn-queue output + the lease table
  2. Generating per-ticket worker prompts (TKT-ORCH-023 compliant:
     ≥6 verifier sub-agents, distinct IDs, WORKER_RESULT schema enforced)
  3. Emitting a JSON manifest the orchestrator (Claude session) reads
  4. Emitting per-ticket worker-prompt text files for inline paste

The orchestrator (Claude session) reads dispatch-manifest.json and
dispatches via Agent tool. This module does NOT dispatch sub-agents
itself (sub-agents require Agent tool which lives inside the Claude session).

Usage:
    python3 -c "from orchestrator.scripts.dispatch import build_dispatch_plan;
                print(build_dispatch_plan(['TKT-DEEP-XXX:123', 'TKT-DEEP-YYY:456']))"
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).parent.parent.parent
TICKETS_DIR = ROOT / "orchestrator" / "tickets"
DISPATCH_DIR = ROOT / ".dispatch"  # ephemeral, gitignored
DISPATCH_MANIFEST = DISPATCH_DIR / "dispatch-manifest.json"
DISPATCH_PROMPTS_DIR = DISPATCH_DIR / "prompts"


@dataclass
class WorkerDispatch:
    ticket_id: str
    lease_token: int
    priority: str = "P0"
    ticket_file: Optional[Path] = None
    prompt_file: Optional[Path] = None
    depends_on: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# READY-FOR-DISPATCH line parser
# ---------------------------------------------------------------------------

DISPATCH_LINE = re.compile(
    r"📦\s+READY-FOR-DISPATCH:\s+(?P<ticket_id>TKT-\S+)\s+fencing_token=(?P<lease_token>\d+)"
)

LEASE_LINE = re.compile(
    r"(?P<ticket_id>TKT-\S+)\s+CLAIMED\s+by\s+(?P<holder>\S+)\s+until\s+(?P<until>\S+)\s+—\s+fencing_token=(?P<ft>\d+)"
)


def parse_dispatch_lines(burn_output: str) -> list[tuple[str, int]]:
    """Extract (ticket_id, lease_token) pairs from READY-FOR-DISPATCH lines."""
    out = []
    for m in DISPATCH_LINE.finditer(burn_output):
        out.append((m.group("ticket_id"), int(m.group("lease_token"))))
    return out


def parse_lease_lines(burn_output: str) -> list[tuple[str, int]]:
    """Extract (ticket_id, fencing_token) pairs from 'CLAIMED' lines."""
    out = []
    for m in LEASE_LINE.finditer(burn_output):
        out.append((m.group("ticket_id"), int(m.group("ft"))))
    return out


# ---------------------------------------------------------------------------
# Worker prompt builder (TKT-ORCH-023 compliant)
# ---------------------------------------------------------------------------

WORKER_PROMPT_HEADER = """## Ticket: {ticket_id}
## Lease token: {lease_token}
## Priority: {priority}
## File scope: {ticket_file}

You are an Astra orchestrator worker dispatched via Agent tool (CLAUDE.md anti-stall #1: small scope).

CRITICAL: do not read OTHER tickets. Burn this one ticket. Write files. Report. That's it.

## Hard rules (CLAUDE.md + TKT-ORCH-023)

1. **WRITE-FILES-FIRST** (anti-stall #2). Don't plan for hours — start implementing immediately.
2. **Heartbeat every 3 tool calls** — write `orchestrator/progress/.heartbeat-{ticket_id}` file (anti-stall #3).
3. **Small scope = 1 ticket per dispatch** (anti-stall #1). Do not read other tickets.
4. **Verifier dispatch (TKT-ORCH-023 + CLAUDE.md change 2026-09-26)**: you MUST dispatch ≥{min_verifiers} distinct verifier sub-agents per the {priority} requirement (≥6 for P0/P1, ≥4 for P2/P3). **Self-attestation is FORBIDDEN.** If you genuinely cannot dispatch the required verifiers due to resource constraints, return `final_block: BLOCKED` with reason `verifier_dispatch_resource_constraint`.
5. Per-tenant isolation (CLAUDE.md #2): `tenant_id NOT NULL` + RLS on every modification.
6. Synthetic data only in dev (CLAUDE.md #3).
7. CDS recommendations are advisory only, never autonomous (CLAUDE.md #4 + ADR-0006).
8. Lease holds for 30 min — extend via heartbeat; release on completion.

## Required return: WORKER_RESULT JSON

```json
{{
  "files_changed": [{{"path": "...", "summary": "..."}}],
  "verification": {{
    "correctness": "PASS|FAIL",
    "per_tenant": "PASS|FAIL",
    "failure_modes": "PASS|FAIL",
    "reversibility": "PASS|FAIL|N/A",
    "security": "PASS|FAIL|N/A",
    "performance": "PASS|FAIL|N/A",
    "idempotency": "PASS|FAIL|N/A",
    "retries": N,
    "verified_at": "ISO timestamp",
    "verifier_provenance": {{
      "dispatched_count": N,
      "sub_agent_ids": ["..."]
    }}
  }},
  "blockers_discovered": [...],
  "final_status": "DONE|PARTIAL|BLOCKED"
}}
```

## Time budget

Target 5-15 min wall-clock. Skip verbose commentary. Write code, run tests, report.

Read the ticket file at:
{ticket_file}

Burn the ticket. Return WORKER_RESULT.
"""


def build_worker_prompt(ticket_id: str, lease_token: int, priority: str = "P0") -> str:
    """Render the worker dispatch prompt for a single ticket.

    TKT-ORCH-023: P0/P1 → ≥6 verifiers, P2/P3 → ≥4. The orchestrator session
    uses this to invoke Agent tool with the rendered prompt.
    """
    min_verifiers = 6 if priority in ("P0", "P1") else 4
    ticket_path = _find_ticket_file(ticket_id)
    ticket_file_rel = str(ticket_path.relative_to(ROOT)) if ticket_path else f"orchestrator/tickets/orch-self/{ticket_id}-*.md"
    body_prompt = WORKER_PROMPT_HEADER.format(
        ticket_id=ticket_id,
        lease_token=lease_token,
        priority=priority,
        min_verifiers=min_verifiers,
        ticket_file=ticket_file_rel,
    )
    return body_prompt


def _find_ticket_file(ticket_id: str) -> Optional[Path]:
    """Return the canonical ticket file path for a ticket_id."""
    for d in [TICKETS_DIR, TICKETS_DIR / "orch-self", TICKETS_DIR / "discovered"]:
        if not d.is_dir():
            continue
        for p in d.glob(f"**/{ticket_id}*.md"):
            return p
    return None


# ---------------------------------------------------------------------------
# Dispatch plan builder
# ---------------------------------------------------------------------------

def build_dispatch_plan(
    claimed_tickets: list[tuple[str, int]],
    priority_map: Optional[dict[str, str]] = None,
    out_dir: Path = DISPATCH_DIR,
) -> list[WorkerDispatch]:
    """Build the dispatch plan and write the manifest + per-ticket prompts.

    Args:
        claimed_tickets: list of (ticket_id, lease_token) pairs
        priority_map:   optional map ticket_id → priority (default: P0)
        out_dir:        where to write .dispatch/dispatch-manifest.json +
                        .dispatch/prompts/<ticket>.txt (gitignored)

    Returns:
        list[WorkerDispatch] — one per claimed ticket, in claim order
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    DISPATCH_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    priority_map = priority_map or {}

    plan: list[WorkerDispatch] = []
    started_at = datetime.now(timezone.utc).isoformat()

    for ticket_id, lease_token in claimed_tickets:
        priority = priority_map.get(ticket_id, "P0")
        ticket_file = _find_ticket_file(ticket_id)
        prompt_text = build_worker_prompt(ticket_id, lease_token, priority)
        prompt_file = DISPATCH_PROMPTS_DIR / f"{ticket_id}.txt"
        prompt_file.write_text(prompt_text)

        plan.append(WorkerDispatch(
            ticket_id=ticket_id,
            lease_token=lease_token,
            priority=priority,
            ticket_file=ticket_file,
            prompt_file=prompt_file,
        ))

    # Manifest
    manifest = {
        "started_at": started_at,
        "ticket_count": len(plan),
        "priority_distribution": _priority_distribution(plan),
        "tickets": [
            {
                "ticket_id": d.ticket_id,
                "lease_token": d.lease_token,
                "priority": d.priority,
                "ticket_file": str(d.ticket_file.relative_to(ROOT)) if d.ticket_file else None,
                "prompt_file": str(d.prompt_file.relative_to(ROOT)) if d.prompt_file else None,
            }
            for d in plan
        ],
    }
    DISPATCH_MANIFEST.write_text(json.dumps(manifest, indent=2))
    return plan


def _priority_distribution(plan: list[WorkerDispatch]) -> dict[str, int]:
    from collections import Counter
    return dict(Counter(d.priority for d in plan))


# ---------------------------------------------------------------------------
# Public dispatch helper (used by orchestrator session)
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    """Load the dispatch manifest written by the orchestrator."""
    if not DISPATCH_MANIFEST.exists():
        return {}
    return json.loads(DISPATCH_MANIFEST.read_text())


def manifest_summary() -> str:
    """One-line summary of the dispatch manifest (for orchestrator session to read)."""
    m = load_manifest()
    if not m:
        return "no dispatch manifest at .dispatch/dispatch-manifest.json"
    return (f"{m['ticket_count']} tickets ready to dispatch "
            f"(started {m['started_at']}); "
            f"priorities: {m['priority_distribution']}")


def all_prompts_text() -> str:
    """Read all per-ticket worker prompts concatenated. Useful for orchestrator Session resume."""
    out = []
    for p in sorted(DISPATCH_PROMPTS_DIR.glob("*.txt")):
        out.append(f"\n{'=' * 70}\n{p.name}\n{'=' * 70}\n{p.read_text()}")
    return "\n".join(out) if out else ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--read-manifest":
        print(manifest_summary())
        sys.exit(0)
    elif len(sys.argv) > 1 and sys.argv[1] == "--read-prompts":
        print(all_prompts_text())
        sys.exit(0)
    else:
        print(__doc__)
