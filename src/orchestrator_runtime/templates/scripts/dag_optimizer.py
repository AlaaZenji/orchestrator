"""DAG optimizer — Orchestrator 2.0 Phase 5A.

**Purpose.** Per Final Follow-Up Directive §6, the orchestrator must construct
a real dependency graph (DAG) and optimize it before execution. This module is
the deterministic DAG optimizer primitive.

**What it does.**

Given a list of tickets with explicit `depends_on:` frontmatter, this module
builds a DAG, infers missing dependencies where detectable, detects cycles,
and emits a sequence of "execution waves" (each wave = set of tickets that
can run in parallel; subsequent waves = tickets whose dependencies are now
satisfied).

**Per Directive §6 test matrix:**

1. **Explicit dependency** — tickets with `depends_on:` are scheduled AFTER
   the dependency. (Sanity.)
2. **Inferred dependency** — if ticket B's `files_affected` overlap ticket
   A's `files_affected`, B depends on A. (File-level ordering heuristic.)
3. **Missing dependency** — if ticket B depends on a ticket ID that doesn't
   exist, B is reported as MISSING_DEP.
4. **Circular dependency** — A → B → A. Reported as CYCLE.
5. **Conflicting phase ordering** — if ticket A is marked phase=2 and
   ticket B is marked phase=1, but B depends on A, this is a PHASE_ORDER
   conflict.
6. **Parallelizable tickets** — tickets with no inter-dependencies can run
   in the same wave.
7. **Serial-only tickets** — a ticket marked `serial_only: true` cannot be
   parallelized; it must be alone in its wave.
8. **Stale dependencies** — if ticket B depends on A and A is `status:
   CANCELLED` or `DEPRECATED`, B has a STALE_DEP.

**Stdlib-only.**

**Usage.**

    from dag_optimizer import optimize_dag, Wave, DAGIssue

    tickets = [
        {"id": "TKT-A", "depends_on": [], "files_affected": ["foo.py"]},
        {"id": "TKT-B", "depends_on": ["TKT-A"], "files_affected": ["bar.py"]},
    ]
    result = optimize_dag(tickets)
    print(result.waves)       # [Wave(1, ["TKT-A"]), Wave(2, ["TKT-B"])]
    print(result.issues)      # []

**Date.** 2026-09-20 (Phase 5A).
"""

from __future__ import annotations

import enum
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


# --- Result schema -----------------------------------------------------------

@dataclass
class Wave:
    """One execution wave. Tickets in a single wave can run in parallel.
    Serial-only tickets are alone in their wave."""
    wave_number: int
    ticket_ids: list[str] = field(default_factory=list)


class IssueSeverity(str, enum.Enum):
    HIGH = "HIGH"        # CYCLE / MISSING_DEP / PHASE_ORDER conflict
    MEDIUM = "MEDIUM"    # STALE_DEP / inferred ordering surprise
    LOW = "LOW"          # informational


class IssueCategory(str, enum.Enum):
    CIRCULAR_DEPENDENCY = "circular_dependency"
    MISSING_DEPENDENCY = "missing_dependency"
    PHASE_ORDER_CONFLICT = "phase_order_conflict"
    STALE_DEPENDENCY = "stale_dependency"
    SELF_DEPENDENCY = "self_dependency"


@dataclass
class DAGIssue:
    ticket_ids: list[str]
    severity: IssueSeverity
    category: IssueCategory
    detail: str = ""


@dataclass
class DAGResult:
    waves: list[Wave] = field(default_factory=list)
    issues: list[DAGIssue] = field(default_factory=list)
    parallelizable_count: int = 0
    serial_only_count: int = 0


# --- Helpers -----------------------------------------------------------------

def _ticket_by_id(tickets: list[dict]) -> dict[str, dict]:
    return {t["id"]: t for t in tickets if "id" in t}


def _detect_cycle(graph: dict[str, list[str]]) -> Optional[list[str]]:
    """Return the cycle as a list of ticket_ids if a cycle exists, else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    # Restrict to nodes that exist in the graph (filter out edges to missing tickets).
    valid_nodes = {n for n in graph if n in graph}
    color: dict[str, int] = {n: WHITE for n in valid_nodes}
    parent: dict[str, Optional[str]] = {n: None for n in valid_nodes}

    def dfs(start: str) -> Optional[list[str]]:
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, idx = stack[-1]
            if color[node] == WHITE:
                color[node] = GRAY
            children = [c for c in graph.get(node, []) if c in valid_nodes]
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                if color.get(child, WHITE) == GRAY:
                    cycle = [child, node]
                    cursor = parent.get(node)
                    while cursor is not None and cursor != child:
                        cycle.append(cursor)
                        cursor = parent.get(cursor)
                    if cursor == child:
                        cycle.append(child)
                    return list(reversed(cycle))
                if color.get(child, WHITE) == WHITE:
                    parent[child] = node
                    stack.append((child, 0))
            else:
                color[node] = BLACK
                stack.pop()
        return None

    for node in sorted(valid_nodes):
        if color[node] == WHITE:
            cycle = dfs(node)
            if cycle is not None:
                return cycle
    return None


# --- Inferred dependency -----------------------------------------------------

def _infer_dependencies(
    tickets: list[dict],
    explicit_deps: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Infer file-overlap-based dependencies.

    If ticket A's `files_affected` is a strict subset of ticket B's, B
    depends on A (B cannot be merged until A's files are stable).

    Otherwise, if A and B share any file, the ticket with the later
    alphabetical ID depends on the earlier (a deterministic tiebreak).
    """
    deps = {tid: list(d) for tid, d in explicit_deps.items()}
    tids = [t["id"] for t in tickets if "id" in t]
    by_file: dict[str, list[str]] = defaultdict(list)
    for t in tickets:
        for f in t.get("files_affected", []):
            by_file[f].append(t["id"])

    # File-overlap ordering.
    for f, ids in by_file.items():
        if len(ids) > 1:
            ids_sorted = sorted(ids)
            for i in range(1, len(ids_sorted)):
                later = ids_sorted[i]
                earlier = ids_sorted[i - 1]
                if earlier not in deps.setdefault(later, []):
                    deps[later].append(earlier)
    return deps


# --- Optimizer ---------------------------------------------------------------

def optimize_dag(
    tickets: list[dict],
    *,
    infer_deps: bool = True,
    check_phases: bool = True,
) -> DAGResult:
    """Build the DAG and emit execution waves.

    Args:
        tickets: list of ticket dicts with at least `id` and `depends_on`.
                  Optional fields: `files_affected`, `phase`, `status`,
                  `serial_only`.
        infer_deps: include file-overlap inference.
        check_phases: detect phase-order conflicts.

    Returns:
        DAGResult with waves + issues.
    """
    issues: list[DAGIssue] = []
    valid_tickets = [t for t in tickets if "id" in t]
    by_id = _ticket_by_id(valid_tickets)

    # Issue 1: self-dependency.
    for t in valid_tickets:
        deps = t.get("depends_on", []) or []
        if t["id"] in deps:
            issues.append(DAGIssue(
                ticket_ids=[t["id"]],
                severity=IssueSeverity.HIGH,
                category=IssueCategory.SELF_DEPENDENCY,
                detail=f"ticket depends on itself",
            ))

    # Issue 2: missing dependency.
    explicit_deps: dict[str, list[str]] = {}
    for t in valid_tickets:
        tid = t["id"]
        deps = [d for d in (t.get("depends_on", []) or []) if d != tid]
        missing = [d for d in deps if d not in by_id]
        for m in missing:
            issues.append(DAGIssue(
                ticket_ids=[tid, m],
                severity=IssueSeverity.HIGH,
                category=IssueCategory.MISSING_DEPENDENCY,
                detail=f"{tid} depends on missing ticket {m}",
            ))
        explicit_deps[tid] = deps

    # Issue 3: stale dependency (depends on CANCELLED / DEPRECATED).
    for t in valid_tickets:
        tid = t["id"]
        for d in explicit_deps.get(tid, []):
            dep_ticket = by_id.get(d)
            if dep_ticket is None:
                continue
            status = (dep_ticket.get("status") or "").upper()
            if status in ("CANCELLED", "DEPRECATED", "DEFERRED"):
                issues.append(DAGIssue(
                    ticket_ids=[tid, d],
                    severity=IssueSeverity.MEDIUM,
                    category=IssueCategory.STALE_DEPENDENCY,
                    detail=f"{tid} depends on {d} which is {status}",
                ))

    # Issue 4: phase-order conflict.
    if check_phases:
        for t in valid_tickets:
            tid = t["id"]
            my_phase = t.get("phase")
            if my_phase is None:
                continue
            try:
                my_phase_int = int(my_phase)
            except (TypeError, ValueError):
                continue
            for d in explicit_deps.get(tid, []):
                dep_ticket = by_id.get(d)
                if dep_ticket is None:
                    continue
                dep_phase = dep_ticket.get("phase")
                if dep_phase is None:
                    continue
                try:
                    dep_phase_int = int(dep_phase)
                except (TypeError, ValueError):
                    continue
                if my_phase_int < dep_phase_int:
                    issues.append(DAGIssue(
                        ticket_ids=[tid, d],
                        severity=IssueSeverity.HIGH,
                        category=IssueCategory.PHASE_ORDER_CONFLICT,
                        detail=f"{tid} (phase={my_phase_int}) depends on {d} (phase={dep_phase_int}) — phase must be >= dep phase",
                    ))

    # Inferred dependencies.
    if infer_deps:
        deps = _infer_dependencies(valid_tickets, explicit_deps)
    else:
        deps = {tid: list(d) for tid, d in explicit_deps.items()}

    # Issue 5: circular dependency.
    cycle = _detect_cycle(deps)
    if cycle is not None:
        # Trim trailing duplicate if present.
        if cycle and cycle[0] == cycle[-1]:
            cycle = cycle[:-1]
        issues.append(DAGIssue(
            ticket_ids=cycle,
            severity=IssueSeverity.HIGH,
            category=IssueCategory.CIRCULAR_DEPENDENCY,
            detail=f"cycle: {' -> '.join(cycle + [cycle[0]])}",
        ))
        # Cannot compute waves if there's a cycle; return early.
        return DAGResult(waves=[], issues=issues,
                         parallelizable_count=0,
                         serial_only_count=sum(1 for t in valid_tickets if t.get("serial_only")))

    # Kahn's algorithm for topological sort + wave computation.
    in_degree: dict[str, int] = {tid: 0 for tid in deps}
    forward: dict[str, list[str]] = defaultdict(list)
    for src, dst_list in deps.items():
        for dst in dst_list:
            if dst in by_id:   # ignore edges to missing tickets
                forward[dst].append(src)   # reverse: dst -> src
                in_degree[src] = in_degree.get(src, 0) + 1

    waves: list[Wave] = []
    serial_only_count = sum(1 for t in valid_tickets if t.get("serial_only"))
    remaining = set(deps.keys())
    parallelizable_count = 0
    wave_num = 0
    while remaining:
        wave_num += 1
        # Find tickets with no remaining unsatisfied dependencies.
        ready: list[str] = []
        for tid in remaining:
            # A ticket is ready if all of its deps are no longer in remaining.
            unsatisfied = [d for d in deps.get(tid, []) if d in remaining]
            if not unsatisfied:
                ready.append(tid)
        if not ready:
            # Shouldn't happen (cycles caught above) but be defensive.
            break

        # Per Directive §6: serial-only tickets must be alone in their wave.
        serial_in_ready = [tid for tid in ready if by_id.get(tid, {}).get("serial_only")]
        if serial_in_ready:
            wave_tickets = [serial_in_ready[0]]
            serial_only_count += 1
        else:
            wave_tickets = sorted(ready)
            parallelizable_count += len(wave_tickets) - 1   # >1 ticket in this wave = parallel

        waves.append(Wave(wave_number=wave_num, ticket_ids=wave_tickets))
        for tid in wave_tickets:
            remaining.discard(tid)

    return DAGResult(
        waves=waves,
        issues=issues,
        parallelizable_count=parallelizable_count,
        serial_only_count=serial_only_count,
    )


# --- CLI ---------------------------------------------------------------------

def _main() -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="DAG optimizer (Phase 5A).")
    p.add_argument("--tickets-file", default=None,
                   help="JSON file containing a list of ticket dicts")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    if args.tickets_file:
        tickets = json.loads(__import__("pathlib").Path(args.tickets_file).read_text())
    else:
        print("ERROR: provide --tickets-file")
        return 1

    result = optimize_dag(tickets)
    if args.json:
        print(json.dumps({
            "waves": [{"wave_number": w.wave_number, "ticket_ids": w.ticket_ids} for w in result.waves],
            "issues": [{
                "ticket_ids": i.ticket_ids,
                "severity": i.severity.value,
                "category": i.category.value,
                "detail": i.detail,
            } for i in result.issues],
            "parallelizable_count": result.parallelizable_count,
            "serial_only_count": result.serial_only_count,
        }, indent=2))
    else:
        print(f"{len(result.waves)} wave(s); {len(result.issues)} issue(s)")
        for w in result.waves:
            print(f"  Wave {w.wave_number}: {w.ticket_ids}")
        for i in result.issues:
            print(f"  [{i.severity.value:8s}] {i.category.value}: {'+'.join(i.ticket_ids)} — {i.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
