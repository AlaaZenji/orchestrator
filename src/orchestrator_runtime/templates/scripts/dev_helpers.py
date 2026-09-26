#!/usr/bin/env python3
"""Small developer helpers used by Astra workers.

Each function is intentionally tiny — extract next migration number, resolve
ticket artifact paths, etc. Why these helpers: empirical burn latency (2026-09-26
session) showed workers spending ~30 sec on `ls db/migrations` +
back-of-envelope counting + manual path composition per ticket. These helpers
collapse that latency to ~1 sec.

Usage:
    python3 -c "from orchestrator.scripts.dev_helpers import next_migration_number, ticket_artifacts_paths; print(next_migration_number())"
    python3 -c "from orchestrator.scripts.dev_helpers import ticket_artifacts_paths; print(ticket_artifacts_paths('TKT-DEEP-XXX'))"
"""
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent.parent  # /Users/diracx/health_ai_2.0
DB_MIGRATIONS = ROOT / "db" / "migrations"
EXTERNAL_VALIDATION = ROOT / "docs" / "03-architecture" / "external-validation"
SUBGROUP_METRICS = ROOT / "orchestrator" / "world-model" / "subgroup-metrics"
DRIFT_MONITORING = ROOT / "orchestrator" / "world-model" / "drift-monitoring"


def next_migration_number(prefix: str = "V") -> int:
    """Return the next free migration number (e.g., 53 if V052 is the latest).

    Used by workers writing Flyway migrations so they don't have to manually
    count `db/migrations/`. Saves ~30 sec per migration + avoids duplicate
    numbering race conditions.

    Args:
        prefix: Migration filename prefix (default 'V' for Flyway).

    Returns:
        int — one greater than the highest existing numbered migration.
    """
    if not DB_MIGRATIONS.is_dir():
        return 1
    nums = []
    for p in DB_MIGRATIONS.glob(f"{prefix}*.sql"):
        m = re.match(rf"{re.escape(prefix)}(\d+)", p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def ticket_artifacts_paths(ticket_id: str) -> dict[str, Path]:
    """Return the canonical artifact paths for a CDS-class ticket per ADR-0028 §8.

    Workers writing §8 artifacts can skip path lookup and write directly to
    the returned paths. Saves ~30 sec per CDS-class ticket burn.

    Returns a dict with keys:
      - 'external_validation': docs/03-architecture/external-validation/<id>.md
      - 'subgroup_metrics':    orchestrator/world-model/subgroup-metrics/<id>.yaml
      - 'drift_monitoring':    orchestrator/world-model/drift-monitoring/<id>.yaml
    """
    return {
        "external_validation": EXTERNAL_VALIDATION / f"{ticket_id}.md",
        "subgroup_metrics": SUBGROUP_METRICS / f"{ticket_id}.yaml",
        "drift_monitoring": DRIFT_MONITORING / f"{ticket_id}.yaml",
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "next-migration":
        print(f"V{next_migration_number():03d}")
    elif len(sys.argv) > 1 and sys.argv[1] == "artifact-paths":
        tid = sys.argv[2] if len(sys.argv) > 2 else "TKT-DEEP-EXAMPLE"
        for k, p in ticket_artifacts_paths(tid).items():
            print(f"  {k}: {p.relative_to(ROOT)}")
    else:
        print(__doc__)
