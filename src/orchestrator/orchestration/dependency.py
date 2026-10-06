"""DAG topological dependency resolver and cycle detector."""

from typing import Dict, List, Set, Collection
from orchestrator.domain.models import Work
from orchestrator.domain.status import WorkStatus
from orchestrator.domain.exceptions import OrchestratorError


class CyclicDependencyError(OrchestratorError):
    """Raised when a circular dependency cycle is detected in the Work graph."""
    def __init__(self, cycle: List[str]):
        super().__init__(
            f"Cyclic dependency detected in work graph: {' -> '.join(cycle)}",
            code="CYCLIC_DEPENDENCY",
            status_code=400,
        )
        self.cycle = cycle


class DependencyResolver:
    """Computes dependency resolution, topological order, and ready-sets for Works."""

    @staticmethod
    def detect_cycles(works: Collection[Work]) -> None:
        """Validates that the dependency graph has no directed cycles.
        Raises CyclicDependencyError if a cycle is found.
        """
        adj: Dict[str, List[str]] = {w.work_id: list(w.dependencies) for w in works}
        visited: Dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited
        path: List[str] = []

        def dfs(node: str) -> None:
            visited[node] = 1
            path.append(node)
            for neighbor in adj.get(node, []):
                state = visited.get(neighbor, 0)
                if state == 1:
                    # Cycle detected
                    idx = path.index(neighbor)
                    cycle = path[idx:] + [neighbor]
                    raise CyclicDependencyError(cycle)
                elif state == 0:
                    dfs(neighbor)
            path.pop()
            visited[node] = 2

        for w in works:
            if visited.get(w.work_id, 0) == 0:
                dfs(w.work_id)

    @staticmethod
    def is_eligible(work: Work, succeeded_work_ids: Set[str]) -> bool:
        """A work is eligible if it is currently PENDING and all its dependencies are SUCCEEDED."""
        if work.status != WorkStatus.PENDING:
            return False
        return all(dep_id in succeeded_work_ids for dep_id in work.dependencies)

    @staticmethod
    def compute_eligible_works(all_works: Collection[Work]) -> List[Work]:
        """Returns all PENDING works whose upstream dependencies are all in SUCCEEDED status."""
        succeeded_ids: Set[str] = {
            w.work_id for w in all_works if w.status == WorkStatus.SUCCEEDED
        }
        eligible = [
            w for w in all_works if DependencyResolver.is_eligible(w, succeeded_ids)
        ]
        # Sort by priority descending, then created_at ascending
        return sorted(eligible, key=lambda w: (-w.priority, w.created_at))
