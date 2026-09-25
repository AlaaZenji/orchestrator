#!/usr/bin/env bash
# Bootstrap orchestrator-runtime in a project at $1 (default: /tmp/example-orch-project).
# Usage: bash bootstrap.sh [target-dir]

set -euo pipefail

TARGET="${1:-/tmp/example-orch-project}"

echo "Bootstrapping orchestrator-runtime in: $TARGET"
echo "=========================================="

rm -rf "$TARGET"
mkdir -p "$TARGET"

orchestrator-setup init \
    --target "$TARGET" \
    --project-name "Example Orchestrator Project" \
    --prefix "ex" \
    --state-store "skip" \
    --initial-state "cold-start" \
    --git-init "yes" \
    --multi-tenant "no" \
    --domain "general"

echo ""
echo "=========================================="
echo "Bootstrap complete. Files in $TARGET/orchestrator/:"
ls "$TARGET/orchestrator/"
echo ""
echo "Slash command installed: /ex-burn (and others)"
echo "Next: bash verify.sh $TARGET"
