#!/usr/bin/env bash
# Verify the orchestrator is wired up correctly.
# Usage: bash verify.sh [target-dir]

set -euo pipefail

TARGET="${1:-/tmp/example-orch-project}"

if [ ! -d "$TARGET" ]; then
    echo "ERROR: $TARGET doesn't exist. Run bootstrap.sh first."
    exit 1
fi

echo "Verifying orchestrator in: $TARGET"
echo "=========================================="

echo ""
echo "=== 1. Burn queue (should show 0 tickets) ==="
python3 "$TARGET/orchestrator/scripts/burn_queue.py" --list-only

echo ""
echo "=== 2. Dependency audit (should show no BLOCKED tickets) ==="
python3 "$TARGET/orchestrator/scripts/dependency_audit.py"

echo ""
echo "=== 3. Doctor (should show all checks PASS) ==="
orchestrator-setup doctor \
    --target "$TARGET" \
    --prefix "ex" \
    --state-store "skip"

echo ""
echo "=========================================="
echo "All verifications passed. The orchestrator is wired correctly."
