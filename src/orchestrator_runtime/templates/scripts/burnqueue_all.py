#!/usr/bin/env python3
"""Burn-queue ALL — 3-stage orchestrator with auto-dispatch.

Stage 1: Run burn_queue until no new tickets claim (drain the queue).
Stage 2: Detect bottlenecks from Stage 1 output + leases + drift tickets;
         auto-file resolution tickets.
Stage 3 (NEW): Build the worker auto-dispatch plan — generate per-ticket
         TKT-ORCH-023 compliant worker prompts + JSON manifest. The
         orchestrator (Claude session) reads .dispatch/dispatch-manifest.json
         and fires Agent tool calls in parallel up to MAX_PARALLEL.

UI: clean ANSI terminal output via `orchestrator/scripts/ui.py`. The slash
command `/burnqueue-all` invokes this and prints the dispatch plan for the
orchestrator session to consume.

Usage:
    python3 orchestrator/scripts/burnqueue_all.py [--limit N] [--no-ui]
    # Then orchestrator session reads:
    python3 -c "from orchestrator.scripts.dispatch import manifest_summary; print(manifest_summary())"
    # To dispatch each:
    python3 -c "from orchestrator.scripts.dispatch import all_prompts_text; print(all_prompts_text())"

Exit codes:
    0 — all done, queue healthy, dispatch ready
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
dispatch = importlib.import_module("dispatch")


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
    claimed = burn_output.count("READY-FOR-DISPATCH")
    skipped = burn_output.count("SKIPPED (no claim)")
    found = burn_output.count("Found ")
    return {"found": found, "claimed": claimed, "skipped": skipped}


def stage1_burn(limit: int, no_ui: bool, started_at: datetime) -> dict:
    """Stage 1: drain the burn queue."""
    if not no_ui:
        ui_module.header(
            started_at_iso=started_at.isoformat(),
            stage_label="Stage 1/3 — Burn Queue (claim)",
        )

    all_claim_rows: list = []
    all_skip_rows: list = []
    totals = {"claimed": 0, "skipped": 0, "completed": 0, "in_progress": 0}
    claimed_tickets: list = []
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
                    "BLOCKER", "<burn_queue>",
                    "ran > 180s — likely a stall. Check watchdog_loop.py.",
                )
            break
        except Exception as e:
            if not no_ui:
                ui_module.bottleneck_finding("BLOCKER", "<burn_queue>", f"spawn failed: {e}")
            break

        if rounds == 1:
            counts = count_tickets_in_output(out)
            initial_tickets = max(counts["found"], 1)

        new_findings = bottleneck_detector.parse_skip_lines(out)
        for f in new_findings:
            auto = bottleneck_detector.file_resolution_ticket(f)
            f.auto_filed = auto
            all_skip_rows.append((f.ticket_id, f.summary, auto or "-"))

        ready_count = out.count("READY-FOR-DISPATCH")
        if ready_count > 0:
            import re
            for m in re.finditer(r"📦 READY-FOR-DISPATCH:\s*(TKT-\S+)\s+fencing_token=(\d+)", out):
                tid = m.group(1)
                ft = int(m.group(2))
                all_claim_rows.append((tid, "██████████ READY", f"fencing_token={ft}"))
                totals["in_progress"] += 1
                claimed_tickets.append((tid, ft))
            totals["claimed"] += ready_count

        if ready_count == 0 and not new_findings:
            break

    if not no_ui:
        ui_module.stage_panel(
            stage_num=1,
            stage_total=3,
            title="Burn Queue (claim)",
            initial_tickets=initial_tickets,
            claimed=totals["claimed"],
            completed=totals["claimed"],  # optimistic
            skipped=len(all_skip_rows),
            blocked=0,
            in_progress=max(totals["in_progress"] - totals["claimed"], 0),
            claim_rows=all_claim_rows[:10],
            skip_rows=all_skip_rows[:10],
        )

    return {
        "initial_tickets": initial_tickets,
        "claim_rows": all_claim_rows,
        "skip_rows": all_skip_rows,
        "stats": totals,
        "burn_output": "".join([out]),
        "claimed_tickets": claimed_tickets,
    }


def stage2_bottlenecks(no_ui: bool, started_at: datetime, claimed_tickets: list) -> dict:
    """Stage 2: detect + auto-file bottleneck resolution tickets."""
    if not no_ui:
        ui_module.header(
            started_at_iso=started_at.isoformat(),
            stage_label="Stage 2/3 — Bottleneck Resolution (auto-file)",
        )

    findings = []
    auto_filed = []
    drift_findings = bottleneck_detector.find_drift_tickets()
    blocker_findings = bottleneck_detector.find_discovery_tickets()
    stale_findings = bottleneck_detector.find_stale_leases()

    # Also include any tickets we just claimed but haven't dispatched —
    # they're "in flight" not stalling until 30 min lease TTL.
    findings = drift_findings + blocker_findings + stale_findings

    for f in findings:
        auto = bottleneck_detector.file_resolution_ticket(f)
        if auto:
            f.auto_filed = auto
            auto_filed.append((f.kind, f.ticket_id, f.summary, auto))

    if not no_ui:
        skip_rows = [
            (f"{f.kind}/{f.ticket_id}", f.summary, f.auto_filed or "-")
            for f in findings
        ]
        ui_module.stage_panel(
            stage_num=2,
            stage_total=3,
            title="Bottleneck Resolution (auto-file)",
            initial_tickets=len(findings) if findings else 1,
            claimed=0,
            completed=0,
            skipped=0,
            blocked=sum(1 for f in findings if f.kind == "BLOCKER"),
            in_progress=0,
            claim_rows=[],
            skip_rows=skip_rows[:10],
        )
        for kind, tid, summary, auto in auto_filed:
            ui_module.auto_filed_notice(tid, summary)

    return {
        "findings": findings,
        "auto_filed": auto_filed,
        "stats": {"drift": len(drift_findings),
                  "blocker": len(blocker_findings),
                  "stale_lease": len(stale_findings)},
        "claimed_tickets": claimed_tickets,
    }


def stage3_dispatch(no_ui: bool, started_at: datetime, claimed_tickets: list) -> dict:
    """Stage 3: build the worker auto-dispatch plan.

    Writes `.dispatch/dispatch-manifest.json` + per-ticket worker prompts.
    The orchestrator (Claude session) reads these and dispatches via
    Agent tool. This module does NOT actually invoke sub-agents — Agent
    tool is only available in the Claude session.
    """
    if not no_ui:
        ui_module.header(
            started_at_iso=started_at.isoformat(),
            stage_label="Stage 3/3 — Worker Auto-Dispatch (manifest ready)",
        )

    if not claimed_tickets:
        if not no_ui:
            ui_module.stage_panel(
                stage_num=3,
                stage_total=3,
                title="Worker Auto-Dispatch (manifest ready)",
                initial_tickets=1,
                claimed=0, completed=0, skipped=0, blocked=0, in_progress=0,
                claim_rows=[],
                skip_rows=[],
            )
        return {"plan": [], "manifest_path": None}

    plan = dispatch.build_dispatch_plan(claimed_tickets)
    manifest_path = dispatch.DISPATCH_MANIFEST

    # Mark Stage 3 in the UI
    if not no_ui:
        claim_rows = [
            (
                d.ticket_id,
                "██████████ READY",
                f"lease_token={d.lease_token}  priority={d.priority}",
            )
            for d in plan
        ]
        ui_module.stage_panel(
            stage_num=3,
            stage_total=3,
            title="Worker Auto-Dispatch (manifest ready)",
            initial_tickets=len(plan),
            claimed=len(plan),
            completed=0,
            skipped=0,
            blocked=0,
            in_progress=len(plan),
            claim_rows=claim_rows,
            skip_rows=[],
        )

    return {"plan": plan, "manifest_path": str(manifest_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args(argv)

    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    try:
        s1 = stage1_burn(limit=args.limit, no_ui=args.no_ui, started_at=started_at)
        s2 = stage2_bottlenecks(no_ui=args.no_ui, started_at=started_at,
                                claimed_tickets=s1.get("claimed_tickets", []))
        s3 = stage3_dispatch(no_ui=args.no_ui, started_at=started_at,
                              claimed_tickets=s1.get("claimed_tickets", []))

        total = max(s1["stats"]["claimed"] + s1["stats"]["skipped"], 1)
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

            print()
            print(ui_module._bold("  ▶ Next: orchestrator session reads the dispatch manifest"))
            print(f"    manifest: {s3.get('manifest_path', '<not written>')}")
            print(f"    prompts:   {dispatch.DISPATCH_PROMPTS_DIR.relative_to(PROJECT_ROOT) if dispatch.DISPATCH_PROMPTS_DIR.exists() else '<none>'}")
            print()
            print(ui_module._bold("  Stage 3 follow-up (orchestrator session):"))
            print(f"    python3 -c \"from orchestrator.scripts.dispatch import manifest_summary; print(manifest_summary())\"")
            print(f"    python3 -c \"from orchestrator.scripts.dispatch import all_prompts_text; print(all_prompts_text())\"")
            print()
            print(ui_module._bold("  → Use Agent tool to dispatch each prompt in parallel."))

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

