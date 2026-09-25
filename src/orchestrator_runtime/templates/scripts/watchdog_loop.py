#!/usr/bin/env python3
"""Watchdog loop — auto-detect stalls + alert orchestrator.

Runs every 60s. For each active heartbeat file:
- If heartbeat > STALL_TIMEOUT_MIN old → print ALERT for orchestrator
- Optionally force-kill + re-dispatch (set FORCE_KILL=1)

Usage:
    python3 orchestrator/scripts/watchdog_loop.py [--timeout-min 3] [--kill]
"""
import sys
import os
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).parent.parent.parent
HEARTBEAT_DIR = ROOT / "orchestrator" / "progress"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-min", type=int, default=3)
    parser.add_argument("--kill", action="store_true", help="Force-kill stalled workers (requires psutil)")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")
    args = parser.parse_args()

    print(f"Watching heartbeats in {HEARTBEAT_DIR} (timeout={args.timeout_min}m, interval={args.interval}s, kill={args.kill})")

    while True:
        if not HEARTBEAT_DIR.exists():
            print("  (heartbeat dir missing — waiting)")
            time.sleep(args.interval)
            continue

        now = datetime.now(timezone.utc).timestamp()
        active = list(HEARTBEAT_DIR.glob(".heartbeat-*"))
        stalled = []
        for hb_file in active:
            age_min = (now - hb_file.stat().st_mtime) / 60
            if age_min > args.timeout_min:
                stalled.append((hb_file, age_min))

        if stalled:
            for hb_file, age_min in stalled:
                ticket_id = hb_file.name.replace(".heartbeat-", "")
                print(f"[{datetime.now(timezone.utc).isoformat()}] ⚠️  STALL: {ticket_id} ({age_min:.1f}m since last heartbeat)")
                if args.kill:
                    try:
                        import psutil
                        # Find PIDs matching ticket_id via process cmdline
                        killed = 0
                        for proc in psutil.process_iter(['cmdline']):
                            try:
                                cmdline = ' '.join(proc.info.get('cmdline', []))
                                if ticket_id in cmdline:
                                    proc.kill()
                                    killed += 1
                            except Exception:
                                pass
                        print(f"    killed {killed} process(es) for {ticket_id}")
                    except ImportError:
                        print(f"    --kill requires psutil (pip install psutil)")
        else:
            print(f"[{datetime.now(timezone.utc).isoformat()}] ✅ all workers fresh")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
