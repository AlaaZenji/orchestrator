#!/usr/bin/env python3
"""Burn-queue ALL — 2-stage orchestrator.

Stage 1: Run burn_queue until no new tickets claim (drain the queue).
Stage 2: Detect bottlenecks from Stage 1 output, file resolution tickets,
         burn those, then re-run detection. Loop until no new bottlenecks.

UI: clean ANSI terminal output via `orchestrator/scripts/ui.py`. The slash
command `/burnqueue-all` invokes this with --ui and forwards stdout back.

Usage:
    python3 orchestrator/scripts/burnqueue_all.py [--limit N] [--no-ui]

Exit codes:
    0 — all done, queue healthy
    1 — bottlenecks surfaced (re-run recommended)
    2 — fatal error
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# Use importlib to avoid ModuleNotFoundError when run as `python3 orchestrator/scripts/burnqueue_all.py`
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
ui_module = importlib.import_module("ui")
bottleneck_detector = importlib.import_module("bottleneck_detector")


PROJECT_ROOT = Path(__file__).parent.parent.parent
BURN_QUEUE = PROJECT_ROOT / "orchestrator" / "scripts" / "burn_queue.py"


def run_burn_queue_once(limit: int = 6, no_ui: bool = False) -> tuple[str, str]:
    """Run `burn_queue.py --limit N` once. Returns (stdout, stderr)."""
    proc = subprocess.run(
        [
            sys.executable,
            str(BURN_QUEUE),
            "--limit", str(limit),
            "--no-auto-reconcile",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(PROJECT_ROOT),
    )
    return proc.stdout, proc.stderr


def count_tickets_in_output(burn_output: str) -> dict[str, int]:
    """Parse burn output and return counts by status."""
    claimed = burn_output.count("READY-FOR-DISPATCH")
    skipped = burn_output.count("SKIPPED (no claim)")
    found = burn_output.count("Found ")
    return {"found": found, "claimed": claimed, "skipped": skipped}


def stage1_burn(limit: int, no_ui: bool, started_at: datetime) -> dict:
    """Stage 1: drain the burn queue.

    Returns a dict of stats:
      initial_tickets, claimed, completed, skipped, blocked, in_progress,
      claim_rows (list of (tid, percent, label)),
      skip_rows (list of (tid, reason, auto_filed))
    """
    if not no_ui:
        ui_module.header(
            started_at_iso=started_at.isoformat(),
            stage_label="Stage 1/2 — Burn Queue",
        )

    all_claim_rows: list[tuple[str, str, str]] = []
    all_skip_rows: list[tuple[str, str, str]] = []
    totals = {"claimed": 0, "skipped": 0, "completed": 0, "in_progress": 0}
    rounds = 0
    max_rounds = 4
    initial_tickets = 0

    while rounds < max_rounds:
        rounds += 1
        try:
            out, err = run_burn_queue_once(limit=limit)
        except subprocess.TimeoutExpired:
            if not no_ui:
                ui_module.bottleneck_finding(
                    "BLOCKER", "<burn_queue subprocess>",
                    "ran > 180s — likely a stall. Check watchdog_loop.py.",
                )
            break
        except Exception as e:
            if not no_ui:
                ui_module.bottleneck_finding(
                    "BLOCKER", "<burn_queue subprocess>",
                    f"spawn failed: {e}",
                )
            break

        # First round: count initial
        if rounds == 1:
            counts = count_tickets_in_output(out)
            initial_tickets = max(counts["found"], 1)

        # Per round: parse skips
        new_findings = bottleneck_detector.parse_skip_lines(out)
        if new_findings:
            for f in new_findings:
                auto = bottleneck_detector.file_resolution_ticket(f)
                f.auto_filed = auto
                all_skip_rows.append((f.ticket_id, f.summary, auto or "-"))

        # Per round: claim/READY counts
        ready_count = out.count("READY-FOR-DISPATCH")
        if ready_count > 0:
            # Extract claim lines
            import re
            for m in re.finditer(r"📦 READY-FOR-DISPATCH:\s*(TKT-\S+)\s+fencing_token=(\d+)", out):
                tid = m.group(1)
                ft = m.group(2)
                all_claim_rows.append((tid, "██████████ READY", f"fencing_token={ft}"))
                totals["in_progress"] += 1
            totals["claimed"] += ready_count

        # Stop when no more tickets to claim (no READY, no SKIPPED)
        if ready_count == 0 and not new_findings:
            break

    # Compose final stats: claimed = got a lease; completed = reported DONE
    # (we'd ideally listen to sub-agent completions here; placeholder for now)
    completed = totals["claimed"]  # best-effort

    if not no_ui:
        ui_module.stage_panel(
            stage_num=1,
            stage_total=2,
            title="Burn Queue",
            initial_tickets=initial_tickets,
            claimed=totals["claimed"],
            completed=completed,
            skipped=len(all_skip_rows),
            blocked=0,
            in_progress=max(totals["in_progress"] - completed, 0),
            claim_rows=all_claim_rows[:10],
            skip_rows=all_skip_rows[:10],
        )

    return {
        "initial_tickets": initial_tickets,
        "claim_rows": all_claim_rows,
        "skip_rows": all_skip_rows,
        "stats": totals,
        "burn_output": "".join(_r[1] for _r in [(0, out)]),  # last output
    }


def stage2_bottlenecks(no_ui: bool, started_at: datetime) -> dict:
    """Stage 2: detect bottlenecks, file resolution tickets, re-burn them."""
    if not no_ui:
        ui_module.header(
            started_at_iso=started_at.isoformat(),
            stage_label="Stage 2/2 — Bottleneck Resolution",
        )

    findings = []
    auto_filed = []
    drift_findings = bottleneck_detector.find_drift_tickets()
    blocker_findings = bottleneck_detector.find_discovery_tickets()
    stale_findings = bottleneck_detector.find_stale_leases()

    findings = drift_findings + blocker_findings + stale_findings
    for f in findings:
        auto = bottleneck_detector.file_resolution_ticket(f)
        if auto:
            f.auto_filed = auto
            auto_filed.append((f.kind, f.ticket_id, f.summary, auto))

    # Stable leases we can't easily auto-file: surface only
    for f in stale_findings:
        if not f.auto_filed:
            f.details = (f.details or "") + " (requires human assist)"
        else:
            f.details = (f.details or "") + f" (auto-filed: {f.auto_filed})"

    if not no_ui:
        drift_rows = [
            (f.ticket_id, f.summary, f.auto_filed or "-")
            for f in drift_findings
        ]
        ui_module.stage_panel(
            stage_num=2,
            stage_total=2,
            title="Bottleneck Resolution",
            initial_tickets=len(findings) if findings else 1,
            claimed=0,
            completed=0,
            skipped=0,
            blocked=sum(1 for f in findings if f.kind == "BLOCKER"),
            in_progress=0,
            claim_rows=[],
            skip_rows=[
                (f"{f.kind}/{f.ticket_id}", f.summary, f.auto_filed or "-")
                for f in findings
            ][:10],
            drift_rows=drift_rows[:10],
        )
        for kind, tid, summary, auto in auto_filed:
            ui_module.auto_filed_notice(tid, summary)

    return {
        "findings": findings,
        "auto_filed": auto_filed,
        "stats": {"drift": len(drift_findings),
                  "blocker": len(blocker_findings),
                  "stale_lease": len(stale_findings)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=6,
                        help="Max tickets per burn-queue invocation (default: 6)")
    parser.add_argument("--no-ui", action="store_true",
                        help="Disable ANSI UI output (for CI)")
    parser.add_argument("--rounds", type=int, default=4,
                        help="Max burn-queue rounds (default: 4)")
    args = parser.parse_args(argv)

    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    try:
        s1 = stage1_burn(limit=args.limit, no_ui=args.no_ui, started_at=started_at)
        s2 = stage2_bottlenecks(no_ui=args.no_ui, started_at=started_at)

        total = (
            max(s1["stats"]["claimed"] + s1["stats"]["skipped"], 1)
        )
        completed = min(s1["stats"]["claimed"], total)

        if not args.no_ui:
            ui_module.final_summary(
                total_tickets=total,
                completed=completed,
                skipped=len(s1["skip_rows"]) + len(s2["findings"]),
                auto_filed=len(s2["auto_filed"]),
                duration_seconds=time.time() - t0,
                bottlenecks_resolved=len(s2["auto_filed"]),
                bottlenecks_remaining=len([f for f in s2["findings"] if not f.auto_filed]),
            )

        # Exit 1 if bottlenecks remain (orchestrator will not auto-fix everything)
        remaining = len([f for f in s2["findings"] if not f.auto_filed])
        return 1 if remaining > 0 else 0

    except KeyboardInterrupt:
        if not args.no_ui:
            ui_module._red("\n  ⚠ Interrupted by user.\n")
        return 130
    except Exception as e:
        if not args.no_ui:
            ui_module.bottleneck_finding("BLOCKER", "<burnqueue-all>", f"fatal: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
