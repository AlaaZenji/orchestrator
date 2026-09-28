#!/usr/bin/env python3
"""Small developer helpers used by orchestrator workers.

Each function is intentionally tiny — extract next migration number, etc.
Why these helpers: empirical burn latency shows workers spending ~30 sec on
`ls db/migrations` + back-of-envelope counting per ticket. These helpers
collapse that latency to ~1 sec.

Usage:
    python3 -c "from orchestrator.scripts.dev_helpers import next_migration_number; print(next_migration_number())"
"""
import re
from pathlib import Path


ROOT = Path(__file__).parent.parent.parent
DB_MIGRATIONS = ROOT / "db" / "migrations"


def next_migration_number(prefix: str = "V") -> int:
    """Return the next free migration number (e.g., 53 if V052 is the latest).

    Used by workers writing Flyway-style migrations so they don't have to
    manually count `db/migrations/`. Saves ~30 sec per migration + avoids
    duplicate numbering race conditions.

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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "next-migration":
        print(f"V{next_migration_number():03d}")
    else:
        print(__doc__)