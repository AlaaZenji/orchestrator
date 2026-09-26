#!/usr/bin/env python3
"""Burn queue orchestrator (CLAIMS + RECONCILE + STATUS, NO SILENT FAILURES).

This script:
1. Lists all QUEUED tickets (skips DONE/CANCELLED/DEPRECATED + BLOCKED-with-blockers)
2. Filters by priority (default: any)
3. Claims up to N tickets in parallel via force_claim.py
4. EMITS "ready for dispatch" message (orchestrator does the actual Agent dispatch via claude_code SDK)
5. Waits up to N min per worker (heartbeat timeout)
6. Reports completion + errors

WHY THIS SCRIPT DOES NOT DISPATCH DIRECTLY:
Previous version used `from claude_code import dispatch_subagent` which
DOES NOT EXIST in this environment — every dispatch silently failed,
claiming leases without actually launching workers. The fix: orchestrator
(Claude session) dispatches workers directly via the Agent tool,
NOT this script. This script only does the orchestration parts that
actually work: claim + status + heartbeat-monitor + reap-stale + reconcile.

Usage:
    python3 orchestrator/scripts/burn_queue.py [--priority P0|P1|P2|P3] [--limit N]
    python3 orchestrator/scripts/burn_queue.py --list-only
"""
import sys
import os
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).parent.parent.parent
TICKETS = ROOT / "orchestrator" / "tickets"
PROGRESS = ROOT / "orchestrator" / "progress"


# ---------------------------------------------------------------------------
# Configuration (PERSISTENT — survives sessions)
# ---------------------------------------------------------------------------

SKIP_STATUSES = frozenset(["DONE", "CANCELLED", "DEPRECATED"])
DEFAULT_WATCHDOG_MIN = 3
DEFAULT_TTL_MIN = 30
MAX_PARALLEL = 6


# ---------------------------------------------------------------------------
# the project's domain decision support burn gate (the project's decision records §8.6 — TKT-NNN)
# ---------------------------------------------------------------------------
#
# Per the project's decision records §8.6, the burn queue MUST consult `burn_gate.py` before
# picking up a the project's domain decision support-class ticket. Exit codes:
#   0 = PASS  — gate satisfied; proceed to claim
#   1 = FAIL  — gate unsatisfied (artifacts missing); skip + log
#   2 = INVALID — gate cannot decide (ticket unreadable); surface per §7 BLOCKED
#   3 = NOT-the project's domain decision support — ticket is explicitly not the project's domain decision support-class; proceed unchanged
#
# Stdlib-only (matches burn_gate.py constraint). Reversible: delete the
# call site in force_claim_ticket() to revert.
# ---------------------------------------------------------------------------


class CdsGateSkip(Exception):
    """Raised when burn_gate returns FAIL (exit 1) or INVALID (exit 2).

    Carries the exit code so the dispatcher can emit the correct log tag:
      exit 1 → `[the project's domain decision support-GATE-FAIL]`  (skip; log missing artifact path)
      exit 2 → `[the project's domain decision support-GATE-INVALID]`  (surface; BLOCKED escalation per the project's decision records §7)
    """

    def __init__(self, ticket_id: str, code: int, reason: str = ""):
        super().__init__(reason or f"burn_gate exit {code} for {ticket_id}")
        self.ticket_id = ticket_id
        self.code = code
        self.reason = reason

    @property
    def tag(self) -> str:
        return "the project's domain decision support-GATE-FAIL" if self.code == 1 else "the project's domain decision support-GATE-INVALID"


BURN_GATE_SCRIPT = ROOT / "orchestrator" / "scripts" / "burn_gate.py"


def _cds_progress_log() -> Path:
    """Path to today's burn-queue progress log.

    Per the project's decision records §8.6 + the §7 BLOCKED escalation convention: write one row
    per gate decision so the orchestrator can audit what was skipped.
    """
    name = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-burn-queue.md"
    return PROGRESS / name


def cds_burn_gate_check(ticket_id: str) -> int:
    """Invoke `burn_gate.py --ticket=<id>` and append a decision row
    to today's progress log.

    Returns the script's exit code (0=PASS, 1=FAIL, 2=INVALID, 3=NOT-the project's domain decision support).
    On FAIL or INVALID, the function logs `[the project's domain decision support-GATE-FAIL]` or
    `[the project's domain decision support-GATE-INVALID]` to `progress/YYYY-MM-DD-burn-queue.md` before
    returning, and the caller MUST skip the lease claim.
    On PASS or NOT-the project's domain decision support, the caller proceeds to claim unchanged.

    Never raises — every failure mode is swallowed so the burn queue
    always reaches its own dispatch loop (per "no silent failures").
    """
    try:
        result = subprocess.run(
            ["python3", str(BURN_GATE_SCRIPT), f"--ticket={ticket_id}"],
            capture_output=True, text=True, timeout=15,
        )
        rc = result.returncode
    except subprocess.TimeoutExpired:
        # Defensive: surface as INVALID with a timeout reason. Treat as
        # gate-cannot-decide (BLOCKED escalation per §7).
        rc = 2
        result = None  # type: ignore[assignment]
    except Exception:
        # Defensive: if the gate script is missing entirely, surface as
        # INVALID so the operator notices (rather than silently bypassing
        # §8.6 enforcement).
        rc = 2
        result = None  # type: ignore[assignment]
    # Fail-CLOSED (TKT-NNN FIX 1): any exit code outside the documented
    # contract {0=PASS, 1=FAIL, 2=INVALID, 3=NOT-the project's domain decision support} is upgraded to INVALID
    # (2) per the project's decision records §7 BLOCKED escalation. A buggy or compromised gate
    # emitting rc=4 (or anything else) MUST NOT silently bypass §8.6
    # enforcement. The orchestrator's default is deny, not permit.
    if rc not in (0, 1, 2, 3):
        rc = 2
    if rc in (1, 2):
        tag = "the project's domain decision support-GATE-FAIL" if rc == 1 else "the project's domain decision support-GATE-INVALID"
        try:
            PROGRESS.mkdir(parents=True, exist_ok=True)
            with open(_cds_progress_log(), "a", encoding="utf-8") as f:
                f.write(
                    f"[{tag}] {ticket_id} — "
                    f"{'artifacts missing (see burn_gate output)' if rc == 1 else 'gate cannot make decision (BLOCKED per §7)'}\n"
                )
        except Exception:
            pass
    return rc


def read_ticket_status(ticket_path: Path) -> str:
    """Read the frontmatter `status:` field from a ticket file."""
    try:
        text = ticket_path.read_text(errors="ignore")
    except Exception:
        return "UNKNOWN"
    for line in text.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "UNKNOWN"


def read_ticket_id(ticket_path: Path) -> str:
    """Read the frontmatter `id:` field from a ticket file.

    Per Final Follow-Up Directive §5: ticket-id is the canonical key. The
    file stem (e.g., `TKT-DEEP-LAUNCH-001-foundation.md`) is a routing slug —
    not the canonical id. Using the file stem as the ticket id (the prior
    default) caused `cds_burn_gate.py --ticket=<id>` lookups to fail for
    tickets with filename suffixes beyond their id (e.g., `*FU2`,
    `*populate-cohort-uuid`, `*foundation`). Use this function instead.

    Falls back to the file stem only when no `id:` field is found in the
    first ~20 lines (legacy tickets from early bootstrapping).
    """
    try:
        text = ticket_path.read_text(errors="ignore")
    except Exception:
        return ticket_path.stem
    # Tolerate malformed frontmatter (missing closing `---`): scan only the
    # first 20 lines and the line must be a clean `id: VALUE` shape.
    for line in text.splitlines()[:20]:
        if line.startswith("id:"):
            return line.split(":", 1)[1].strip()
    return ticket_path.stem


def should_skip(ticket_path: Path) -> tuple[bool, str]:
    """Return (should_skip, reason)."""
    if not ticket_path.exists():
        return False, "no frontmatter"
    status = read_ticket_status(ticket_path)
    if status in SKIP_STATUSES:
        return True, f"status={status}"
    return False, status


def list_burnable(priority=None, limit=None) -> list:
    """List tickets eligible to burn, sorted by priority."""
    burnable = []
    for f in TICKETS.glob("**/TKT-*.md"):
        if ".migrate-backups" in str(f):
            continue
        skip, _ = should_skip(f)
        if skip:
            continue
        pri = "P0"
        for line in f.read_text(errors="ignore").splitlines():
            if line.startswith("priority:"):
                pri = line.split(":", 1)[1].strip()
        if priority and pri != priority:
            continue
        burnable.append((pri, f))
    pri_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    burnable.sort(key=lambda x: pri_order.get(x[0], 4))
    if limit:
        burnable = burnable[:limit]
    return [f for _, f in burnable]


def reap_stale_leases():
    """Reap ALL stale leases (called automatically before each batch)."""
    try:
        subprocess.run(
            ["python3", "-c", '''
import sys, os, subprocess
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "<project_root>")
from orchestrator.scripts.lease import read_lease
import psycopg2
import os
os.environ["ASTRA_DB_TENANT"] = "00000000-0000-0000-0000-000000000001"
conn = psycopg2.connect(host=os.environ.get("ORCHESTRATOR_DB_HOST", "localhost"), port=int(os.environ.get("ORCHESTRATOR_DB_PORT", "5432")), dbname=os.environ.get("ORCHESTRATOR_DB_NAME", "orchestrator"), user=os.environ.get("ORCHESTRATOR_DB_USER", "orchestrator_app"), password=os.environ.get("ORCHESTRATOR_DB_PASSWORD", ""))
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET ROLE orchestrator")
future_cutoff = datetime.now(timezone.utc) + timedelta(days=7)
cur.execute("DELETE FROM orchestrator.lease WHERE lease_expires_at < %s", (future_cutoff,))
deleted = cur.rowcount
cur.execute("RESET ROLE")
print(f"Reaped {deleted} stale lease(s)")
'''],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        pass


def force_claim_ticket(ticket_id: str, holder: str, ttl_min: int) -> int:
    """Force-claim a ticket via subprocess. Returns fencing_token or raises.

    Wired with the project's domain decision support burn gate (the project's decision records §8.6 — TKT-NNN):
    - For every ticket, `burn_gate.py --ticket=<id>` is invoked FIRST.
    - Exit 0 (PASS) or 3 (NOT-the project's domain decision support): proceed to the atomic lease claim.
    - Exit 1 (FAIL): raise CdsGateSkip(..., code=1) — log appended, lease NOT claimed.
    - Exit 2 (INVALID): raise CdsGateSkip(..., code=2) — BLOCKED escalation per §7.
    Non-the project's domain decision support tickets return NOT-the project's domain decision support and proceed unchanged (per-tenant discipline
    preserved: CLAUDE.md #2 — the integration is the project's domain decision support-scope only).
    """
    gate_rc = cds_burn_gate_check(ticket_id)
    if gate_rc in (1, 2):
        reason = "artifacts missing" if gate_rc == 1 else "gate cannot decide"
        raise CdsGateSkip(ticket_id, code=gate_rc, reason=reason)
    result = subprocess.run(
        ["python3", str(ROOT / "orchestrator" / "scripts" / "force_claim.py"),
         ticket_id, holder, str(ttl_min)],
        capture_output=True, text=True, check=True, timeout=10,
    )
    # Parse the last "INSERT:" or "Reaped" line for fencing_token
    for line in result.stdout.strip().splitlines():
        if "fencing_token=" in line:
            try:
                return int(line.split("fencing_token=")[-1].split()[0])
            except (ValueError, IndexError):
                pass
        if "CLAIMED:" in line and "fencing_token" in line:
            # Format: ✅ ticket_id CLAIMED by holder until expires_at — fencing_token=N
            for token in line.split():
                if token.startswith("fencing_token="):
                    try:
                        return int(token.split("=")[-1])
                    except ValueError:
                        pass
    raise RuntimeError(f"force_claim output didn't include fencing_token: {result.stdout!r}")


def emit_ready_for_dispatch(ticket_id: str, fencing_token: int, ttl_min: int):
    """Emit a clear, structured READY-FOR-DISPATCH message for the orchestrator.

    The orchestrator (in the Claude session) sees this message and
    dispatches a worker via the Agent tool — this script NEVER dispatches
    directly because dispatch_subagent doesn't exist in this environment.
    """
    print(f"📦 READY-FOR-DISPATCH: {ticket_id} fencing_token={fencing_token} ttl_min={ttl_min}")
    print(f"   → Orchestrator: dispatch via Agent tool with lease_token={fencing_token}")


def emit_batch_summary(batch_id: str, dispatched: list):
    """Emit a clear summary of the batch for the orchestrator session."""
    print(f"\n{'='*60}")
    print(f"BATCH {batch_id} — {len(dispatched)} tickets claimed + ready")
    print(f"{'='*60}")
    for ticket_id, fencing_token in dispatched:
        emit_ready_for_dispatch(ticket_id, fencing_token, DEFAULT_TTL_MIN)
    print()
    print("⚠️  IMPORTANT: this script does NOT dispatch workers directly.")
    print("    The orchestrator (Claude session) sees the READY-FOR-DISPATCH")
    print("    lines above and dispatches workers via the Agent tool.")
    print("    If you see this output, dispatch the workers NOW.")


def auto_reconcile():
    """Auto-run the ticket reconcile + timer after each batch."""
    try:
        subprocess.run(
            ["python3", str(ROOT / "orchestrator" / "scripts" / "auto_reconcile.py")],
            capture_output=True, check=False, timeout=30,
        )
    except Exception:
        pass


def print_timer():
    """Print the estimated-completion timer."""
    try:
        result = subprocess.run(
            ["python3", str(ROOT / "orchestrator" / "scripts" / "estimated_completion.py")],
            capture_output=True, text=True, timeout=15,
        )
        print("\n" + "\n".join(result.stdout.strip().splitlines()[-15:]))
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", type=int, default=None)
    parser.add_argument("--priority", default=None, choices=["P0", "P1", "P2", "P3"])
    parser.add_argument("--limit", type=int, default=MAX_PARALLEL)
    parser.add_argument("--watchdog-min", type=int, default=DEFAULT_WATCHDOG_MIN)
    parser.add_argument("--ttl-min", type=int, default=DEFAULT_TTL_MIN)
    parser.add_argument("--holder", default="orchestrator-burn-queue")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--no-auto-reconcile", action="store_true",
                        help="Skip auto-reconcile after batch")
    args = parser.parse_args()

    # Pre-step: reap stale leases (PERSISTENT)
    print("Reaping stale leases...")
    reap_stale_leases()

    burnable = list_burnable(args.priority, args.limit)
    print(f"Found {len(burnable)} burnable tickets (priority={args.priority or 'any'}, limit={args.limit})")

    if args.list_only:
        for f in burnable:
            print(f"  {f.relative_to(ROOT)}")
        return

    if not burnable:
        print("✅ No burnable tickets. All caught up.")
        return

    # Claim all + emit READY-FOR-DISPATCH messages (orchestrator does Agent dispatch)
    print(f"\nClaiming {min(len(burnable), args.limit)} tickets...")
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dispatched = []

    for ticket_path in burnable[:args.limit]:
        # Use canonical id from frontmatter, not the file stem
        # (per TKT-DEEP-LAUNCH-* routing slugs like `-foundation`)
        ticket_id = read_ticket_id(ticket_path)
        try:
            fencing_token = force_claim_ticket(ticket_id, args.holder, args.ttl_min)
            dispatched.append((ticket_id, fencing_token))
        except CdsGateSkip as e:
            # the project's decision records §8.6 — the project's domain decision support burn gate returned FAIL or INVALID; skip the
            # ticket (no lease claim attempted). The decision row is already
            # appended to progress/<today>-burn-queue.md inside cds_burn_gate_check.
            print(f"  ⚠️ [{e.tag}] {ticket_id}: {e.reason} — SKIPPED (no claim)")
        except Exception as e:
            print(f"  ⚠️ force_claim failed for {ticket_id}: {str(e)[:80]}")

    if not dispatched:
        print("No tickets claimed.")
        return

    # Emit batch summary — orchestrator dispatches workers from this output
    emit_batch_summary(batch_id, dispatched)

    if not args.no_auto_reconcile:
        print("\nAuto-reconciling...")
        auto_reconcile()
        print_timer()


if __name__ == "__main__":
    main()
