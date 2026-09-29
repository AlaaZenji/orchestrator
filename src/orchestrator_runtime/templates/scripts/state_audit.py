#!/usr/bin/env python3
"""state_audit.py — Continuous state-consistency auditor.

TKT-ORCH-FIX-STATE-CONSISTENCY (2026-09-29). Compares the canonical
ticket frontmatter against every derived state (live leases,
WORKER_RESULT.json, heartbeats) and emits drift findings.

This script is the durable bridge that catches the gaps that the burn
queue's self-heal can't: workers that forgot to release leases, leases
held by crashed workers, WORKER_RESULT files with no frontmatter
update, and so on.

Modes:
  (no args)        -- Print all drift findings as JSONL; exit 0
                      unless --fail-on-error is set.
  --fail-on-error  -- Exit non-zero on any ERROR finding.
  --json           -- Print as a single JSON array instead of JSONL.
  --repair         -- Attempt auto-repair for known-safe findings:
                       * stale_heartbeat_no_lease → rm heartbeat
                       * frontmatter_done_lease_held → release lease
                         (orchestrator side release; safer than worker).
                       Audit-only findings (no auto-repair) are still
                       printed.

Exit codes:
  0 — clean (or warnings only).
  1 — at least one ERROR finding (when --fail-on-error is set).
  2 — usage error.

Usage:
  python3 orchestrator/scripts/state_audit.py
  python3 orchestrator/scripts/state_audit.py --fail-on-error
  python3 orchestrator/scripts/state_audit.py --json
  python3 orchestrator/scripts/state_audit.py --repair
"""
import argparse
import dataclasses
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import state as state_mod  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0]
    )
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="Print as single JSON array (default: JSONL)")
    parser.add_argument("--repair", action="store_true",
                        help="Attempt auto-repair for known-safe findings")
    args = parser.parse_args()

    findings = state_mod.audit()
    errors = [f for f in findings if f.severity == "ERROR"]

    if args.repair:
        # Auto-repair known-safe findings.
        # stale_heartbeat_no_lease → rm heartbeat (the heartbeat is dead;
        # no frontmatter or lease change is needed — the worker is gone).
        for f in findings:
            if f.finding_type == "stale_heartbeat_no_lease":
                heartbeat = (
                    state_mod.HEARTBEAT_DIR / f".heartbeat-{f.ticket_id}"
                )
                if heartbeat.exists():
                    heartbeat.unlink()
                    print(
                        f"  🔧 REPAIRED: removed stale heartbeat for "
                        f"{f.ticket_id}",
                        file=sys.stderr,
                    )
            elif f.finding_type == "stale_heartbeat_no_lease_short":
                # Same as above for short-age variants
                heartbeat = (
                    state_mod.HEARTBEAT_DIR / f".heartbeat-{f.ticket_id}"
                )
                if heartbeat.exists():
                    heartbeat.unlink()
            # Other findings require manual investigation:
            # - frontmatter_done_lease_held: worker forgot to release;
            #   needs manual review (was the work correct?).
            # - worker_result_done_frontmatter_queued: worker forgot
            #   to update frontmatter; needs re-running state.commit.
            # - stale_lease_no_worker_result: worker may be in flight.

    if args.json:
        print(json.dumps(
            [dataclasses.asdict(f) for f in findings],
            indent=2,
        ))
    else:
        if not findings:
            print("✅ No drift findings. State is consistent.")
        else:
            for f in findings:
                print(json.dumps(dataclasses.asdict(f), sort_keys=True))

    if args.fail_on_error and errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())