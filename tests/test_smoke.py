"""Smoke tests for orchestrator-runtime.

Run after `pip install -e .`:
    python3 -m pytest tests/ -v

These tests verify the core bootstrap + doctor path works end-to-end
without external dependencies (Postgres/SQLite are tested separately).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CLI = shutil.which("orchestrator-setup")
if CLI is None:
    # Fall back to python -m for editable installs
    CLI = [sys.executable, "-m", "orchestrator_runtime.cli"]


def run_cli(*args, expect_ok=True, cwd=None):
    """Run the orchestrator-setup CLI and return the parsed JSON result."""
    if isinstance(CLI, str):
        cmd = [CLI] + list(args)
    else:
        cmd = CLI + list(args)
    if "--json" not in args:
        cmd.append("--json")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=60)
    if expect_ok and result.returncode != 0:
        pytest.fail(f"CLI failed: {result.stderr}\nstdout: {result.stdout}")
    if not result.stdout.strip():
        pytest.fail(f"CLI produced no output. stderr: {result.stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"CLI output was not valid JSON: {result.stdout[:500]}")


@pytest.fixture
def fresh_project(tmp_path):
    """A fresh, empty directory for bootstrap tests."""
    return tmp_path


def test_cli_help():
    """The CLI should print its help when invoked with --help."""
    result = subprocess.run(
        [CLI] + ["--help"] if isinstance(CLI, str) else CLI + ["--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "init" in result.stdout
    assert "doctor" in result.stdout
    assert "install-cli" in result.stdout


def test_bootstrap_cold_start(fresh_project):
    """Bootstrap a fresh project with cold-start should write 19 files."""
    result = run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "Test Project",
        "--prefix", "test",
        "--state-store", "skip",
        "--initial-state", "cold-start",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )
    assert result["ok"] is True
    assert result["mode"] == "install"
    assert result["git"]["initialized"] is False
    assert result["next_action"] == "/test-ticket"
    # 11 scripts + 1 anti-stall prompt + 3 docs + CLAUDE.md + 4 derived state files = ~19
    assert len(result["files_written"]) >= 15


def test_bootstrap_creates_correct_files(fresh_project):
    """Bootstrap should create the expected orchestrator directory structure."""
    run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "Structure Test",
        "--prefix", "struct",
        "--state-store", "skip",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )

    # Required directories
    assert (fresh_project / "orchestrator" / "scripts").exists()
    assert (fresh_project / "orchestrator" / "prompts").exists()
    assert (fresh_project / "orchestrator" / "docs").exists()
    assert (fresh_project / "orchestrator" / "tickets").exists()
    assert (fresh_project / "orchestrator" / "progress").exists()
    assert (fresh_project / "orchestrator" / "state").exists()

    # Required files
    assert (fresh_project / "CLAUDE.md").exists()
    assert (fresh_project / "orchestrator" / "ARCHITECTURE.md").exists()
    assert (fresh_project / "orchestrator" / "WORKFLOW.md").exists()
    assert (fresh_project / "orchestrator" / "CONVENTIONS.md").exists()
    assert (fresh_project / "orchestrator" / "tickets-index.md").exists()
    assert (fresh_project / "orchestrator" / "NEXT-ACTIONS.md").exists()
    assert (fresh_project / "orchestrator" / "blockers-index.md").exists()
    assert (fresh_project / ".claude" / "settings.local.json").exists()

    # Required runtime scripts (11)
    scripts_dir = fresh_project / "orchestrator" / "scripts"
    expected_scripts = {
        "burn_queue.py", "lease.py", "heartbeat.py", "watchdog.py",
        "watchdog_loop.py", "force_claim.py", "outbox.py", "outbox_consumer.py",
        "ticket_state_machine.py", "dependency_audit.py", "verdict_conflict.py",
        "dag_optimizer.py",  # bonus — 12 scripts total in the engine
    }
    actual_scripts = {p.name for p in scripts_dir.glob("*.py")}
    missing = expected_scripts - actual_scripts
    assert not missing, f"missing scripts: {missing}"


def test_doctor_passes_after_bootstrap(fresh_project):
    """Doctor should report all checks PASS after a successful bootstrap."""
    run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "Doctor Test",
        "--prefix", "doc",
        "--state-store", "skip",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )

    result = run_cli(
        "doctor",
        "--target", str(fresh_project),
        "--prefix", "doc",
        "--state-store", "skip",
        cwd=str(fresh_project),
    )
    assert result["ok"] is True
    check_names = {c["name"] for c in result["checks"]}
    assert "engine_scripts" in check_names
    assert "slash_commands_global" in check_names
    assert "burn_queue_dry_run" in check_names


def test_burn_queue_dry_run(fresh_project):
    """The burn_queue.py script should run --list-only after bootstrap."""
    run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "Burn Test",
        "--prefix", "burn",
        "--state-store", "skip",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )

    result = subprocess.run(
        [sys.executable, str(fresh_project / "orchestrator" / "scripts" / "burn_queue.py"), "--list-only"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "Found 0 burnable tickets" in result.stdout


def test_re_sync_is_idempotent(fresh_project):
    """Re-running init in re-sync mode should not duplicate files or break the project."""
    run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "Sync Test",
        "--prefix", "sync",
        "--state-store", "skip",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )
    files_after_first = set(p.relative_to(fresh_project) for p in fresh_project.rglob("*") if p.is_file())

    # Re-run
    run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "Sync Test",
        "--prefix", "sync",
        "--mode", "re-sync",
        "--state-store", "skip",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )
    files_after_second = set(p.relative_to(fresh_project) for p in fresh_project.rglob("*") if p.is_file())

    # Re-sync may add new files (slash command, derived state) but must not delete
    assert files_after_first.issubset(files_after_second)


def test_no_project_specific_content_in_repo():
    """The repo source tree must not contain any project-specific terms."""
    repo_root = Path(__file__).parent.parent
    src_dir = repo_root / "src"
    forbidden = [
        "health_ai", "Astra", "clinical", "HL7", "FHIR", "MLLP",
        "patient", "hospital", "CDS", "RCM", "TrakCare", "Waystar",
        "Mirth", "Lebanon", "Bed Management", "Command Center",
        "Slice #1", "slice-critique", "Wave 2.5", "PILOT-",
    ]
    found = []
    for f in src_dir.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".md", ".sql"):
            text = f.read_text(errors="ignore")
            for term in forbidden:
                if term in text:
                    found.append((f.relative_to(repo_root), term))
    assert not found, f"Project-specific content found: {found}"


def test_cli_does_not_install_per_prefix_slash_commands(fresh_project):
    """The package must NOT install per-prefix slash commands (project-agnostic promise)."""
    # The user's global commands dir is at ~/.claude/commands/
    # After bootstrap, no <prefix>-*.md files should be there
    global_commands = Path.home() / ".claude" / "commands"

    # Clean any leftover prefix commands from prior tests
    for f in global_commands.glob("test-*.md"):
        f.unlink()
    for f in global_commands.glob("struct-*.md"):
        f.unlink()
    for f in global_commands.glob("doc-*.md"):
        f.unlink()
    for f in global_commands.glob("burn-*.md"):
        f.unlink()
    for f in global_commands.glob("sync-*.md"):
        f.unlink()

    # Bootstrap
    run_cli(
        "init",
        "--target", str(fresh_project),
        "--project-name", "No Slashes Test",
        "--prefix", "no-slashes",
        "--state-store", "skip",
        "--git-init", "no",
        "--multi-tenant", "no",
        "--domain", "general",
        cwd=str(fresh_project),
    )

    # Check that no per-prefix slash commands were installed
    leftover = list(global_commands.glob("no-slashes-*.md"))
    assert not leftover, f"per-prefix slash commands should NOT be installed: {leftover}"
