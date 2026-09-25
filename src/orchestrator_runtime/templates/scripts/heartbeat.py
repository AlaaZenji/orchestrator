#!/usr/bin/env python3
"""Worker heartbeat helper — prevents stream-watchdog stalls.

Workers write a heartbeat file every 5 tool calls. The orchestrator can
check this file to verify progress without polling the worker runtime.

Usage:
    python3 orchestrator/scripts/heartbeat.py <ticket_id> <status_message>
"""
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent.parent.parent
HEARTBEAT_DIR = ROOT / "orchestrator" / "progress"


def heartbeat(ticket_id: str, message: str):
    """Write a heartbeat marker for the given ticket."""
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb_file = HEARTBEAT_DIR / f".heartbeat-{ticket_id}"
    now = datetime.now(timezone.utc).isoformat()
    line = f"{now} | ALIVE: {message}\n"
    with open(hb_file, "a") as f:
        f.write(line)
    # Don't print — let the worker stay quiet
    return str(hb_file)


def list_heartbeats():
    """List all active heartbeats (for orchestrator health check)."""
    if not HEARTBEAT_DIR.exists():
        return []
    hbs = []
    for f in HEARTBEAT_DIR.glob(".heartbeat-*"):
        if f.stat().st_mtime < (datetime.now(timezone.utc).timestamp() - 600):
            # Stale (>10 min old)
            continue
        hbs.append((f.name.replace(".heartbeat-", ""), f.stat().st_mtime))
    return hbs


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 orchestrator/scripts/heartbeat.py <ticket_id> <message>")
        print("       python3 orchestrator/scripts/heartbeat.py --list")
        sys.exit(1)
    if sys.argv[1] == "--list":
        for tid, mtime in list_heartbeats():
            print(f"  {tid} (last beat: {datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()})")
        return
    print(heartbeat(sys.argv[1], " ".join(sys.argv[2:])))


if __name__ == "__main__":
    main()
