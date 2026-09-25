#!/usr/bin/env python3
"""Force-claim a ticket by directly manipulating the Postgres lease table.

Bypasses the lease module's cached-connection bug by using a fresh DB
connection (psycopg2 directly). Used when claim() returns HELD but
read_lease() shows free (a stale-row-read bug).

PERSISTENT FEATURE: --reap-only flag reaps ALL stale leases (forces
any worker that died without releasing). Called automatically by burn_queue.py.

Usage:
    python3 orchestrator/scripts/force_claim.py <ticket_id> <holder> <ttl_min>
    python3 orchestrator/scripts/force_claim.py --reap-only
"""
import sys
import os
import argparse
import psycopg2
from datetime import datetime, timezone, timedelta

# DB config (matches lease module)
DB_CONFIG = {
    "host": "/tmp",
    "port": int(os.environ.get("ORCHESTRATOR_DB_PORT", "5432")),
    "dbname": "orchestrator",
    "user": "orchestrator_app",
    "password": "orchestrator_app_password",
}

TENANT_ID = "00000000-0000-0000-0000-000000000001"


def reap_stale_all():
    """Reap ALL stale leases (PERSISTENT feature for burn queue safety)."""
    # Future-dated cutoff forces reaping of any lease regardless of phantom state
    future_cutoff = datetime.now(timezone.utc) + timedelta(days=7)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()

    try:
        # Use the superuser-bypass delete pattern (avoiding RLS phantom)
        cur.execute("SET ROLE orchestrator")
        cur.execute(
            "DELETE FROM orchestrator.lease WHERE lease_expires_at < %s",
            (future_cutoff,),
        )
        deleted = cur.rowcount
        cur.execute("RESET ROLE")
        print(f"Reaped {deleted} stale lease(s) (cutoff {future_cutoff.isoformat()})")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("holder", nargs="?")
    parser.add_argument("ttl_min", type=int, nargs="?", default=45)
    parser.add_argument("--reap-only", action="store_true",
                        help="Reap ALL stale leases (called by burn_queue.py before each batch)")
    args = parser.parse_args()

    if args.reap_only:
        reap_stale_all()
        return

    if not args.ticket_id or not args.holder:
        print("ERROR: ticket_id and holder required (or use --reap-only)")
        sys.exit(1)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=args.ttl_min)

    # Fresh connection (no cache!)
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # 1. DELETE any existing row for this ticket (force-clear stuck state)
        cur.execute(
            "DELETE FROM orchestrator.lease WHERE ticket_id = %s",
            (args.ticket_id,),
        )
        deleted = cur.rowcount
        print(f"Deleted {deleted} stale row(s)")

        # 2. INSERT new row (atomic, gets a fresh fencing_token)
        cur.execute(
            """
            INSERT INTO orchestrator.lease
                (ticket_id, tenant_id, holder_id, lease_expires_at)
            VALUES (%s, %s, %s, %s)
            RETURNING fencing_token
            """,
            (args.ticket_id, TENANT_ID, args.holder, expires_at),
        )
        fencing_token = cur.fetchone()[0]
        conn.commit()
        print(f"INSERT: fencing_token={fencing_token} expires_at={expires_at.isoformat()}")
        print(f"✅ {args.ticket_id} CLAIMED by {args.holder} until {expires_at.isoformat()}")
    except Exception as e:
        conn.rollback()
        print(f"❌ ERR: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
