#!/usr/bin/env python3
"""Watchdog — monitors sub-agent heartbeats + intervenes on stalls.

Checks heartbeat freshness for in-flight tickets. If no heartbeat in 10 min,
prints alert for orchestrator to intervene. Does NOT auto-kill (orchestrator decides).

Usage:
    python3 orchestrator/scripts/watchdog.py [--check-once] [--timeout-min 10]
"""
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent.parent.parent
HEARTBEAT_DIR = ROOT / "orchestrator" / "progress"


def check_once(timeout_min: int = 10):
    """Check all in-flight tickets for heartbeat freshness. Return list of stalled."""
    if not HEARTBEAT_DIR.exists():
        return []
    stalled = []
    now = datetime.now(timezone.utc).timestamp()
    for hb_file in HEARTBEAT_DIR.glob(".heartbeat-*"):
        age_min = (now - hb_file.stat().st_mtime) / 60
        if age_min > timeout_min:
            ticket_id = hb_file.name.replace(".heartbeat-", "")
            stalled.append((ticket_id, age_min, hb_file))
    return stalled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-once", action="store_true", help="Check once and exit")
    parser.add_argument("--timeout-min", type=int, default=10, help="Stall timeout in minutes")
    parser.add_argument("--loop", action="store_true", help="Continuous monitoring (every 60s)")
    args = parser.parse_args()

    if args.check_once or not args.loop:
        stalled = check_once(args.timeout_min)
        if not stalled:
            print("✅ All workers fresh (within --timeout-min)")
        else:
            print(f"⚠️  {len(stalled)} STALLED worker(s):")
            for ticket_id, age_min, hb_file in stalled:
                print(f"  - {ticket_id} (last heartbeat: {age_min:.1f} min ago)")
        return

    import time
    while True:
        stalled = check_once(args.timeout_min)
        if stalled:
            print(f"[{datetime.now(timezone.utc).isoformat()}] ⚠️  {len(stalled)} STALLED")
            for ticket_id, age_min, _ in stalled:
                print(f"  - {ticket_id} ({age_min:.1f} min)")
        time.sleep(60)


if __name__ == "__main__":
    main()
