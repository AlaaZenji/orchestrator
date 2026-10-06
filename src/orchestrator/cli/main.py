"""Unified Command Line Interface for the Orchestrator Platform."""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from orchestrator.domain.models import Work, ActorContext
from orchestrator.domain.status import ActorRole
from orchestrator.storage.sqlite import SQLiteStorageBackend
from orchestrator.api.service import OrchestratorService


def get_default_service(db_path: Optional[Path] = None) -> OrchestratorService:
    target_db = db_path or (Path.cwd() / ".orchestrator" / "state.db")
    storage = SQLiteStorageBackend(target_db)
    return OrchestratorService(storage=storage)


async def cli_create(args: argparse.Namespace) -> None:
    service = get_default_service(args.db)
    deps = tuple(args.deps.split(",")) if args.deps else ()
    work = await service.create_work(
        work_id=args.work_id,
        title=args.title,
        description=args.description or "",
        priority=args.priority,
        dependencies=deps,
    )
    print(f"Created Work: {work.work_id} [{work.status.value}] '{work.title}'")


async def cli_list(args: argparse.Namespace) -> None:
    service = get_default_service(args.db)
    works = await service.storage.list_works()
    if not works:
        print("No works found.")
        return
    print(f"{'WORK ID':<15} {'STATUS':<20} {'PRIO':<6} {'TOKEN':<8} {'TITLE'}")
    print("-" * 75)
    for w in sorted(works, key=lambda x: (-x.priority, x.created_at)):
        tok = str(w.active_fencing_token) if w.active_fencing_token else "-"
        print(f"{w.work_id:<15} {w.status.value:<20} {w.priority:<6} {tok:<8} {w.title}")


async def cli_status(args: argparse.Namespace) -> None:
    service = get_default_service(args.db)
    work = await service.storage.get_work(args.work_id)
    if not work:
        print(f"Error: Work '{args.work_id}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Work ID:       {work.work_id}")
    print(f"Title:         {work.title}")
    print(f"Status:        {work.status.value}")
    print(f"Priority:      {work.priority}")
    print(f"Fencing Token: {work.active_fencing_token}")
    print(f"Retries:       {work.retry_count} / {work.max_retries}")
    print(f"Dependencies:  {', '.join(work.dependencies) if work.dependencies else 'None'}")

    lease = await service.storage.get_lease(work.work_id)
    if lease:
        print(f"Active Lease:  Holder={lease.holder_id}, Expires={lease.expires_at.isoformat()}")
    else:
        print("Active Lease:  None")

    execs = await service.storage.list_executions(work.work_id)
    print(f"\nExecution History ({len(execs)} attempts):")
    for e in execs:
        print(f"  Attempt {e.attempt}: [{e.status.value}] Token={e.fencing_token}, Worker={e.worker_id}")


async def cli_reconcile(args: argparse.Namespace) -> None:
    service = get_default_service(args.db)
    recovered = await service.run_reconciliation_sweep()
    print(f"Reconciliation complete: {recovered} stalled works recovered.")


async def cli_approve(args: argparse.Namespace) -> None:
    service = get_default_service(args.db)
    actor = ActorContext(actor_id="operator", role=ActorRole.HUMAN_OPERATOR)
    work = await service.approve_work(args.work_id, actor)
    print(f"Work '{work.work_id}' approved! Status is now {work.status.value}.")


async def cli_cancel(args: argparse.Namespace) -> None:
    service = get_default_service(args.db)
    actor = ActorContext(actor_id="operator", role=ActorRole.HUMAN_OPERATOR)
    work = await service.cancel_work(args.work_id, args.reason or "Cancelled by operator", actor)
    print(f"Work '{work.work_id}' cancelled.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Durable AI Agent Orchestration Platform — Production Reference Implementation",
    )
    parser.add_argument("--db", type=Path, help="Path to SQLite database file (default: .orchestrator/state.db)")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a new Work unit")
    p_create.add_argument("work_id", help="Unique identifier for the work (e.g. TKT-101)")
    p_create.add_argument("title", help="Title / objective of the work")
    p_create.add_argument("--description", "-d", default="", help="Detailed description")
    p_create.add_argument("--priority", "-p", type=int, default=0, help="Priority (higher executes first)")
    p_create.add_argument("--deps", help="Comma-separated list of upstream dependency work IDs")

    # list
    p_list = subparsers.add_parser("list", help="List all Work units")

    # status
    p_status = subparsers.add_parser("status", help="Show detailed status of a Work unit")
    p_status.add_argument("work_id", help="Work identifier")

    # reconcile
    p_rec = subparsers.add_parser("reconcile", help="Run crash recovery reconciliation sweep")

    # approve
    p_app = subparsers.add_parser("approve", help="Grant human approval to a paused Work unit")
    p_app.add_argument("work_id", help="Work identifier")

    # cancel
    p_can = subparsers.add_parser("cancel", help="Cancel a Work unit")
    p_can.add_argument("work_id", help="Work identifier")
    p_can.add_argument("--reason", "-r", default="Cancelled via CLI", help="Cancellation reason")

    args = parser.parse_args()

    commands = {
        "create": cli_create,
        "list": cli_list,
        "status": cli_status,
        "reconcile": cli_reconcile,
        "approve": cli_approve,
        "cancel": cli_cancel,
    }

    cmd_fn = commands.get(args.subcommand)
    if cmd_fn:
        asyncio.run(cmd_fn(args))


if __name__ == "__main__":
    main()
