"""orchestrator-setup — CLI entry point for the orchestrator bootstrap.

Subcommands:
    init      Full bootstrap of the orchestrator into a project directory.
    sync      Re-sync the engine namespace only (idempotent).
    upgrade   Like sync, but bumps engine version metadata.
    doctor    Verify wiring; print what's wired and what's missing.
    install-cli  Install the global slash commands (per-prefix variants).

This CLI replaces the inline bash heredoc that the `/setup-project` slash
command used to invoke. It exists as a standalone script so the slash
command stays simple, the logic is testable, and there's no shell-escape hell.

Exit codes:
    0 = success
    1 = user error (bad args, target doesn't exist)
    2 = engine source error (couldn't find orchestrator_runtime)
    3 = partial failure (some files written, some failed)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Any

# --- Constants --------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).parent
TEMPLATES = PACKAGE_ROOT / "templates"
GLOBAL_SLASH_COMMANDS = PACKAGE_ROOT / "global_slash_commands"

# NOTE: This package ships only /setup-project globally. Per-prefix slash
# commands (e.g. <prefix>-burn) are intentionally NOT shipped — the user
# writes their own domain-specific commands after bootstrap. This keeps
# the orchestrator truly project-agnostic.

# Engine-source resolution order. The package itself is the only source —
# no project-specific fallback. Vendored dirs are a developer convenience.
ENGINE_PRIORITY = [
    ("pip", lambda: _detect_pip()),
    ("vendored", lambda: _detect_vendored()),
]

VENDORED_CANDIDATES = [
    Path.home() / "code" / "orchestrator-runtime" / "src" / "orchestrator_runtime",
    Path.home() / "orchestrator-runtime" / "src" / "orchestrator_runtime",
]


def _detect_pip():
    """Find the installed package."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("orchestrator_runtime")
        if spec and spec.origin:
            return Path(spec.origin).parent / "templates"
    except Exception:
        pass
    return None


def _detect_vendored():
    """Find the templates dir in common vendored locations."""
    for cand in VENDORED_CANDIDATES:
        templates = cand / "templates"
        if templates.exists() and (templates / "scripts" / "burn_queue.py").exists():
            return templates
    return None


def resolve_templates() -> Tuple[Optional[str], Optional[Path]]:
    """Return (source_kind, templates_dir) per the priority list."""
    for kind, detector in ENGINE_PRIORITY:
        root = detector()
        if root is not None:
            return kind, root
    return None, None


# --- Subcommands ------------------------------------------------------------

def cmd_init(args):
    """Full bootstrap. Returns a result dict."""
    target = Path(args.target).resolve()
    if not target.exists() or not target.is_dir():
        return _err(1, f"target does not exist or is not a directory: {target}")

    source_kind, templates_dir = resolve_templates()
    if templates_dir is None:
        return _err(
            2,
            "could not locate orchestrator_runtime. Install with:\n"
            "  pip install orchestrator-runtime\n"
            "or clone from https://github.com/AlaaZenji/orchestrator and use vendored mode."
        )

    result = {
        "ok": True,
        "mode": args.mode,
        "engine_source": f"{source_kind}:{templates_dir}",
        "target": str(target),
        "files_written": [],
        "files_skipped": [],
        "warnings": [],
        "state_store": {"kind": args.state_store, "migrated": False, "tables": []},
        "git": {"initialized": False, "commit_sha": None},
        "next_action": None,
    }

    # 1. Create directory tree
    dirs = [
        target / "orchestrator" / "scripts",
        target / "orchestrator" / "progress",
        target / "orchestrator" / "tickets",
        target / "orchestrator" / "world-model",
        target / "orchestrator" / "blockers",
        target / "orchestrator" / "prompts",
        target / "orchestrator" / "context",
        target / "orchestrator" / "state",
        target / ".claude" / "commands",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 2. Copy engine scripts
    scripts_src = templates_dir / "scripts"
    scripts_dst = target / "orchestrator" / "scripts"
    if args.mode == "install":
        copied = _copy_tree(scripts_src, scripts_dst, suffix=".py")
        result["files_written"].extend(copied)
    elif args.mode in ("re-sync", "upgrade"):
        copied = _copy_tree(scripts_src, scripts_dst, suffix=".py")
        result["files_written"].extend([f for f in copied if not f.endswith("(skipped)")])
    if args.mode == "upgrade":
        version_file = target / "orchestrator" / ".engine-version"
        version_file.write_text(datetime.now(timezone.utc).isoformat())
        result["files_written"].append(str(version_file))

    # 3. Copy engine prompts (just the anti-stall template)
    prompts_src = templates_dir / "prompts"
    prompts_dst = target / "orchestrator" / "prompts"
    if prompts_src.exists():
        copied = _copy_tree(prompts_src, prompts_dst, suffix=".md")
        result["files_written"].extend(copied)

    # 4. Write CLAUDE.md (only if install mode and not present)
    claude_md = target / "CLAUDE.md"
    if args.mode == "install" and not claude_md.exists():
        claude_md.write_text(_render_claude_md(args))
        result["files_written"].append(str(claude_md))
    elif claude_md.exists():
        result["files_skipped"].append("CLAUDE.md (kept existing)")

    # 5. Copy doc spine
    for doc_name in ("ARCHITECTURE.md", "WORKFLOW.md", "CONVENTIONS.md"):
        src_doc = templates_dir / "docs" / doc_name
        if src_doc.exists():
            dst_doc = target / "orchestrator" / doc_name
            if not dst_doc.exists() or args.mode in ("re-sync", "upgrade"):
                shutil.copy(src_doc, dst_doc)
                result["files_written"].append(str(dst_doc))

    # 6. Install the global setup-project slash command (always global)
    setup_cmd = install_setup_slash_command()
    if setup_cmd:
        result["files_written"].append(setup_cmd)

    # 7. Wire SessionStart hook
    _wire_session_start_hook(target, original_path=args.target)
    result["files_written"].append(str(target / ".claude" / "settings.local.json"))

    # 8. State store init
    state_result = _init_state_store(target, args, templates_dir)
    result["state_store"] = state_result
    if state_result.get("warnings"):
        result["warnings"].extend(state_result["warnings"])

    # 9. Initialize derived state
    _init_derived_state(target, args)

    # 10. Enhanced discovery
    if args.initial_state == "discover":
        n_tickets = _enhanced_discovery(target, args)
        result["tickets_seeded"] = n_tickets
        result["next_action"] = f"/{args.prefix}-work"
    elif args.initial_state == "import":
        result["next_action"] = f"/{args.prefix}-burn --list-only"
    else:
        result["next_action"] = f"/{args.prefix}-ticket"

    # 11. Git init
    if args.git_init == "yes" and not (target / ".git").exists():
        try:
            subprocess.run(["git", "init", "-q"], cwd=target, check=True)
            subprocess.run(["git", "add", "."], cwd=target, check=True)
            sha = subprocess.run(
                [
                    "git", "-c", "user.name=setup-project", "-c", "user.email=setup@local",
                    "commit", "-q", "-m",
                    f"Bootstrap orchestrator: {args.prefix}\n\n"
                    f"Set up via /setup-project. Engine source: {result['engine_source']}."
                ],
                cwd=target, capture_output=True, text=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True
            ).stdout.strip()
            result["git"] = {"initialized": True, "commit_sha": sha}
        except subprocess.CalledProcessError as e:
            result["warnings"].append(f"git init/commit failed: {e}")

    return result


def cmd_sync(args):
    args.mode = "re-sync"
    return cmd_init(args)


def cmd_upgrade(args):
    args.mode = "upgrade"
    return cmd_init(args)


def cmd_install_cli(args):
    """Install /setup-project to ~/.claude/commands/ (the only global command)."""
    installed = install_setup_slash_command()
    return {"ok": True, "files_written": [installed] if installed else []}


def cmd_doctor(args):
    """Verify wiring; print what's wired and what's missing."""
    target = Path(args.target).resolve()
    checks = []

    n_scripts = len(list((target / "orchestrator" / "scripts").glob("*.py"))) if (target / "orchestrator" / "scripts").exists() else 0
    checks.append({"name": "engine_scripts", "ok": n_scripts >= 11, "value": n_scripts, "expected": "≥11"})

    n_cmds = len(list(Path.home().glob(f".claude/commands/{args.prefix}-*.md"))) if args.prefix else 0
    # The orchestrator-runtime package is minimal — it ships only /setup-project
    # globally. Per-prefix slash commands (e.g. <prefix>-burn) are project-specific
    # and written by the user. So we expect 0 prefix-specific commands.
    checks.append({"name": "slash_commands_global", "ok": True, "value": n_cmds, "expected": "0 (minimal — write your own)"})

    for doc in ("CLAUDE.md", "orchestrator/ARCHITECTURE.md", "orchestrator/WORKFLOW.md", "orchestrator/CONVENTIONS.md"):
        p = target / doc
        checks.append({"name": f"doc:{doc}", "ok": p.exists(), "value": str(p), "expected": "exists"})

    settings = target / ".claude" / "settings.local.json"
    has_hook = False
    if settings.exists():
        try:
            data = json.loads(settings.read_text())
            hooks = data.get("hooks", {}).get("SessionStart", [])
            has_hook = any("burn_queue" in str(h) for h in hooks)
        except Exception:
            pass
    checks.append({"name": "session_start_hook", "ok": has_hook, "value": str(settings), "expected": "burn_queue hook present"})

    state_pg = (target / "orchestrator" / "state" / "PG_DEFERRED.txt").exists()
    state_sqlite = (target / "orchestrator" / "state.db").exists()
    state_file = (target / "orchestrator" / "state" / "lease.jsonl").exists()
    if state_pg:
        state_kind, state_ok = "postgres (deferred)", True
    elif state_sqlite:
        state_kind, state_ok = "sqlite", True
    elif state_file:
        state_kind, state_ok = "file", True
    elif args.state_store == "skip":
        state_kind, state_ok = "skip (explicit)", True
    else:
        state_kind, state_ok = "unknown", False
    checks.append({"name": "state_store", "ok": state_ok, "value": state_kind, "expected": "one of postgres/sqlite/file/skip"})

    try:
        proc = subprocess.run(
            ["python3", str(target / "orchestrator" / "scripts" / "burn_queue.py"), "--list-only"],
            cwd=target, capture_output=True, text=True, timeout=10,
        )
        checks.append({"name": "burn_queue_dry_run", "ok": proc.returncode == 0, "value": proc.stdout.strip()[:120], "expected": "exit 0"})
    except Exception as e:
        checks.append({"name": "burn_queue_dry_run", "ok": False, "value": str(e), "expected": "exit 0"})

    all_ok = all(c["ok"] for c in checks)
    return {"ok": all_ok, "checks": checks}


# --- Slash command installation --------------------------------------------

def install_setup_slash_command() -> Optional[str]:
    """Install /setup-project to ~/.claude/commands/ if not already present."""
    src = GLOBAL_SLASH_COMMANDS / "setup-project.md"
    if not src.exists():
        return None
    dst = Path.home() / ".claude" / "commands" / "setup-project.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy(src, dst)
    return str(dst)


# --- Helpers ----------------------------------------------------------------

def _copy_tree(src: Path, dst: Path, suffix: Optional[str] = None) -> List[str]:
    written = []
    if not src.exists():
        return written
    dst.mkdir(parents=True, exist_ok=True)
    for src_file in src.rglob("*"):
        if src_file.is_dir() or "__pycache__" in src_file.parts:
            continue
        if suffix and not src_file.name.endswith(suffix):
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        if dst_file.exists():
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src_file, dst_file)
        written.append(str(dst_file))
    return written


def _render_claude_md(args) -> str:
    """Render a CLAUDE.md from the args. Generic, project-agnostic defaults."""
    name = args.project_name
    prefix = args.prefix
    mt = "yes" if args.multi_tenant == "yes" else "no"

    invariants = [
        "**Reversibility.** Every automated recommendation has an undo path. No autonomous irreversible actions.",
        "**Idempotency.** Writes are guarded by `idempotency_key` or `INSERT ... ON CONFLICT`.",
        "**No secrets in code.** Use environment variables; rotate quarterly.",
        "**Tests on every PR.** Unit + integration minimum.",
        "**Type safety** (the project's primary UI language, Python type hints, the project's primary systems language types) — no untyped exports.",
        "**Lint clean** — `pnpm lint` (or equivalent) passes before merge.",
        "**External validation** for any model that ships to users.",
        "**Outbox-first** for any operator action — never lose a user request, even if downstream fails.",
    ]

    invariants_md = "\n".join(f"{i+1}. {inv}" for i, inv in enumerate(invariants))

    return f"""# CLAUDE.md — Engineering Working Notes

> This is the engineering operating manual for **{name}**. Bootstrapped via `/setup-project`.

## Non-negotiables (every commit must respect these)

{invariants_md}

> Each invariant is enforced by a CI gate (run `/{prefix}-fitness` to verify).

## Per-language standards

- (Edit this section with the project's actual languages and frameworks.)

## Per-tenant discipline (only if multi-tenant={mt})

- `tenant_id` as the first column of every primary key.
- Per-tenant CMK envelope encryption via cloud-provider KMS.
- Per-tenant cache namespacing, per-tenant audit-log schema.
- No global mutable state. No cross-tenant shortcuts. No shared caches.

## Architecture references

- `orchestrator/ARCHITECTURE.md` — architectural invariants, autonomy posture, verification discipline.
- `orchestrator/WORKFLOW.md` — Steps 0-7 of the ticket lifecycle.
- `orchestrator/CONVENTIONS.md` — status enum, ticket schema, lease protocol.
- `orchestrator/prompts/anti_stall_prompt_template.md` — anti-stall worker prompt.

## Burn-queue pattern

Use `/{prefix}-burn` for any sub-agent dispatch. The pattern enforces:
- 1 ticket per dispatch (small scope)
- WRITE-FILES-FIRST (no plan-then-stall)
- Heartbeat every 3 tool calls
- No verifier sub-agents inline (self-attested)
- 3-min watchdog timeout
- 30-min lease TTL
"""


def _wire_session_start_hook(target: Path, original_path: Optional[str] = None):
    settings_path = target / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    hook_target = original_path if original_path else str(target)
    hook_cmd = f"python3 {hook_target}/orchestrator/scripts/burn_queue.py --list-only --limit 10"

    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except Exception:
            data = {}
    else:
        data = {}

    data.setdefault("permissions", {})
    data["permissions"].setdefault("defaultMode", "bypassPermissions")

    hooks = data.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])
    already = any("burn_queue.py --list-only" in str(h) for h in session_start)
    if not already:
        session_start.append({"matcher": "**", "hooks": [{"type": "command", "command": hook_cmd}]})

    settings_path.write_text(json.dumps(data, indent=2))


def _init_state_store(target: Path, args, templates_dir: Path) -> Dict[str, Any]:
    result = {"kind": args.state_store, "migrated": False, "tables": [], "warnings": []}

    if args.state_store == "skip":
        return result

    state_dir = target / "orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.state_store == "postgres":
        if not args.postgres_url:
            result["warnings"].append("--state-store=postgres but --postgres-url not provided")
            (state_dir / "PG_DEFERRED.txt").write_text(
                "Postgres migration deferred: --postgres-url not provided at bootstrap time.\n"
                "Run migrations manually when ready.\n"
            )
            return result
        # Use admin URL for migrations if provided, else fall back to app URL
        admin_url = getattr(args, "admin_postgres_url", None) or args.postgres_url
        try:
            user, password, host, port, dbname = _parse_pg_url(admin_url)
            if dbname is None:
                result["warnings"].append(f"could not parse admin postgres-url: {admin_url}")
                return result
            # Fall back to OS user when no user in URL — Unix socket trust auth uses peer identity
            if user is None:
                import getpass
                user = getpass.getuser()

            mig_dir = templates_dir / "migrations" / "postgres"
            for mig in ("V001__lease.sql", "V002__outbox.sql"):
                mig_file = mig_dir / mig
                if not mig_file.exists():
                    continue
                env = os.environ.copy()
                if password:
                    env["PGPASSWORD"] = password
                cmd = ["psql", "-h", host, "-p", port, "-U", user, "-d", dbname, "-v", "ON_ERROR_STOP=1", "-f", str(mig_file)]
                proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
                combined_output = proc.stdout + proc.stderr
                has_error = "ERROR:" in combined_output
                if proc.returncode == 0 and not has_error:
                    result["tables"].append(mig.replace(".sql", ""))
                else:
                    err_line = ""
                    for line in combined_output.splitlines():
                        if "ERROR:" in line:
                            err_line = line.strip()
                            break
                    result["warnings"].append(f"{mig} failed: {err_line or proc.stderr[:200]}")
            result["migrated"] = len(result["tables"]) > 0 and not result["warnings"]
        except Exception as e:
            result["warnings"].append(f"postgres migration error: {e}")

    elif args.state_store == "sqlite":
        db_path = target / "orchestrator" / "state.db"
        try:
            sql = (templates_dir / "migrations" / "sqlite" / "V001__orchestrator.sql").read_text()
            proc = subprocess.run(
                ["sqlite3", str(db_path), sql],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                result["migrated"] = True
                result["tables"] = ["orchestrator_lease", "orchestrator_outbox"]
            else:
                result["warnings"].append(f"sqlite3 failed: {proc.stderr[:200]}")
        except FileNotFoundError:
            result["warnings"].append("sqlite3 CLI not installed; skipping sqlite init")
        except Exception as e:
            result["warnings"].append(f"sqlite migration error: {e}")

    elif args.state_store == "file":
        (state_dir / "lease.jsonl").touch()
        (state_dir / "outbox.jsonl").touch()
        (state_dir / "FILE_MODE_WARN.txt").write_text(
            "file-mode lease/outbox is best-effort. The current engine's lease.py / outbox.py are\n"
            "Postgres-only. Track: TKT-NNN.\n"
        )
        result["tables"] = ["lease.jsonl", "outbox.jsonl"]
        result["migrated"] = True

    return result


def _init_derived_state(target: Path, args):
    tickets_index = target / "orchestrator" / "tickets-index.md"
    if not tickets_index.exists() or tickets_index.stat().st_size < 100:
        tickets_index.write_text(_render_tickets_index(args))

    next_actions = target / "orchestrator" / "NEXT-ACTIONS.md"
    if not next_actions.exists() or next_actions.stat().st_size < 100:
        next_actions.write_text(_render_next_actions(args))

    blockers_index = target / "orchestrator" / "blockers-index.md"
    if not blockers_index.exists() or blockers_index.stat().st_size < 100:
        blockers_index.write_text("# Blockers Index\n\n_No blockers._\n")


def _render_tickets_index(args) -> str:
    return f"""# Tickets Index — {args.project_name}

> Master table of every ticket. Tickets get filed here as `DRAFT` from `/{args.prefix}-ticket`, then `QUEUED` once they enter the burn queue. Mark `DONE` only after accepted + verified + landed per `orchestrator/WORKFLOW.md §5–§6`.

| ID | Title | Status | Priority | Owner | Phase | Updated |
|----|-------|--------|----------|-------|-------|---------|
| _(empty)_ | | | | | | |
"""


def _render_next_actions(args) -> str:
    return f"""# Next Actions — {args.project_name}

> Live queue of work to burn. The orchestrator's burn-queue dispatcher reads this file top-down. Top QUEUED entry is what `/{args.prefix}-burn` will pick up next.

## Top of queue

(empty — run `/{args.prefix}-fill-queue` to discover TODOs, or `/{args.prefix}-ticket` to file one)
"""


def _enhanced_discovery(target: Path, args) -> int:
    passes = {
        "pass1_todos": _grep_pass(
            target,
            ["TODO", "FIXME", "XXX", "HACK", "TBD"],
            ["*.ts", "*.tsx", "*.js", "*.jsx", "*.py", "*.go", "*.rs", "*.swift", "*.java", "*.kt", "*.md"],
            exclude_dirs={"node_modules", ".next", "target", "dist", "build", ".git", "orchestrator", "__pycache__"},
        ),
        "pass2_drift_markers": _grep_pass(
            target,
            ["[UNVERIFIED ESTIMATE]", "[LEGAL WALL]", "[PERSONA WALL]", "[INTERNAL CONTRADICTION]", "[COUNT MISMATCH]"],
            ["*.md"], exclude_dirs={"node_modules", "orchestrator"},
        ),
        "pass3_readme_phases": _grep_pass(
            target, [], ["README.md", "architecture.md"],
            regex_pattern=r"P[0-9]\s*[—-]|Phase\s+[0-9]",
            exclude_dirs={"node_modules", "orchestrator"},
        ),
        "pass4_tbd_sections": _grep_pass(
            target,
            ["TBD", "to be (determined|done|written|implemented)", "placeholder", "stub"],
            ["*.md"], exclude_dirs={"node_modules", "orchestrator"},
        ),
    }

    seeded = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    for origin, hits in passes.items():
        for hit in hits:
            if n >= 20:
                break
            slug = _slugify(hit["text"][:50])
            if not slug:
                continue
            ticket_path = target / "orchestrator" / "tickets" / f"TKT-DISC-{n+1:03d}-{slug}.md"
            if ticket_path.exists():
                continue
            content = f"""---
id: TKT-DISC-{n+1:03d}
title: {hit['text'][:80]}
phase: P0
priority: P3
status: QUEUED
created: {today}
updated: {today}
owner: {args.prefix}-team
adr_refs: []
depends_on: []
estimated_effort: 4h
---

# {hit['text'][:80]}

## Source
`{hit['file']}:{hit['line']}` — `{hit['text']}`

## Acceptance criteria
- [ ] Investigate the discovered issue
- [ ] Either fix it or document why it's acceptable

## Notes
Generated by `/{args.prefix}-fill-queue` enhanced discovery. Origin: {origin}.
"""
            ticket_path.write_text(content)
            seeded.append(ticket_path)
            n += 1

    if seeded:
        for script in ("auto_reconcile.py", "regenerate_next_actions.py"):
            sp = target / "orchestrator" / "scripts" / script
            if sp.exists():
                try:
                    subprocess.run(["python3", str(sp)], cwd=target, capture_output=True, timeout=10)
                except Exception:
                    pass

    return len(seeded)


def _grep_pass(target, patterns, globs, regex_pattern=None, exclude_dirs=None):
    import re as _re
    exclude_dirs = exclude_dirs or set()
    hits = []
    if not patterns and not regex_pattern:
        return hits
    pattern_re = regex_pattern or "|".join(_re.escape(p) for p in patterns)
    try:
        proc = subprocess.run(
            ["grep", "-rn", "-E", pattern_re, str(target),
             f"--include={globs[0]}"] + [f"--include={g}" for g in globs[1:]] +
             [f"--exclude-dir={d}" for d in exclude_dirs],
            capture_output=True, text=True, timeout=15,
        )
        for line in proc.stdout.splitlines()[:50]:
            if ":" not in line:
                continue
            file_part, _, text = line.partition(":")
            line_num_s, _, text = text.partition(":")
            try:
                line_num = int(line_num_s)
            except ValueError:
                continue
            hits.append({"file": file_part, "line": line_num, "text": text.strip()})
    except Exception:
        pass
    return hits


def _slugify(text):
    import re as _re
    s = text.lower()
    s = _re.sub(r"[^a-z0-9\s-]", "", s)
    s = _re.sub(r"[\s_]+", "-", s)
    s = _re.sub(r"-+", "-", s).strip("-")
    return s[:50]


def _err(code: int, msg: str) -> Dict[str, Any]:
    return {"ok": False, "error_code": code, "error": msg}


def _parse_pg_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Parse a Postgres URL into (user, password, host, port, dbname).

    Returns (None, ...) on parse failure. Supports:
      - postgresql://user:pass@host:port/db (TCP)
      - postgresql://user:pass@/db?host=/path&port=N (Unix socket via query)
      - postgresql:///db?host=/path&port=N (no user; caller falls back to OS user)
    """
    m = re.match(r"^postgresql://(?:([^:@]+)(?::([^@]*))?@)?([^:?]*)(?::(\d+))?/(\S+?)(?:\?(.*))?$", url)
    if not m:
        return None, None, None, None, None
    user_m, password_m, host_m, port_m, dbname_m, query_m = m.groups()
    user = user_m  # None if not in URL — caller falls back to OS user
    password = password_m or ""
    host = host_m or "localhost"
    port = port_m or "5432"
    dbname = dbname_m or "orchestrator"
    if query_m:
        for kv in query_m.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k == "host":
                    host = v
                elif k == "port":
                    port = v
    return user, password, host, port, dbname


# --- Main -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Orchestrator setup entry-point")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Full bootstrap")
    p_init.add_argument("--target", required=True)
    p_init.add_argument("--project-name", required=True)
    p_init.add_argument("--prefix", required=True)
    p_init.add_argument("--mode", default="install", choices=["install", "re-sync", "upgrade"])
    p_init.add_argument("--postgres-url", default=None, help="App DSN (read/write; needs SELECT/INSERT/UPDATE/DELETE on orchestrator.* tables)")
    p_init.add_argument("--admin-postgres-url", default=None, help="Admin DSN for running migrations (must own the target schema). Defaults to --postgres-url if not given.")
    p_init.add_argument("--multi-tenant", default="no", choices=["yes", "no"])
    p_init.add_argument("--state-store", default="skip", choices=["postgres", "sqlite", "file", "skip"])
    p_init.add_argument("--git-init", default="no", choices=["yes", "no"])
    p_init.add_argument("--initial-state", default="cold-start", choices=["cold-start", "discover", "import"])
    p_init.add_argument("--domain", default="general")
    p_init.add_argument("--json", action="store_true")
    p_init.set_defaults(func=cmd_init)

    # sync
    p_sync = subparsers.add_parser("sync", help="Re-sync engine namespace only")
    for arg in ("--target", "--project-name", "--prefix", "--postgres-url", "--multi-tenant",
                "--state-store", "--git-init", "--initial-state", "--domain"):
        default = None
        if arg == "--multi-tenant":
            default = "no"
        elif arg == "--state-store":
            default = "skip"
        elif arg == "--git-init":
            default = "no"
        elif arg == "--initial-state":
            default = "cold-start"
        elif arg == "--domain":
            default = "general"
        kwargs = {"required": arg in ("--target", "--project-name", "--prefix"), "default": default}
        p_sync.add_argument(arg, **kwargs)
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # upgrade
    p_up = subparsers.add_parser("upgrade", help="Sync + bump engine version")
    for arg in ("--target", "--project-name", "--prefix", "--postgres-url", "--multi-tenant",
                "--state-store", "--git-init", "--initial-state", "--domain"):
        default = None
        if arg == "--multi-tenant":
            default = "no"
        elif arg == "--state-store":
            default = "skip"
        elif arg == "--git-init":
            default = "no"
        elif arg == "--initial-state":
            default = "cold-start"
        elif arg == "--domain":
            default = "general"
        kwargs = {"required": arg in ("--target", "--project-name", "--prefix"), "default": default}
        p_up.add_argument(arg, **kwargs)
    p_up.add_argument("--json", action="store_true")
    p_up.set_defaults(func=cmd_upgrade)

    # install-cli
    p_inst = subparsers.add_parser("install-cli", help="Install global slash commands with the chosen prefix")
    p_inst.add_argument("--prefix", required=True)
    p_inst.add_argument("--project-name", default="the project")
    p_inst.add_argument("--json", action="store_true")
    p_inst.set_defaults(func=cmd_install_cli)

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Verify wiring")
    p_doc.add_argument("--target", required=True)
    p_doc.add_argument("--prefix", default=None)
    p_doc.add_argument("--state-store", default="skip", choices=["postgres", "sqlite", "file", "skip"])
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    result = args.func(args)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        if result.get("ok"):
            print(f"✓ {args.command} succeeded")
            for k, v in result.items():
                if k in ("ok", "error", "error_code"):
                    continue
                if isinstance(v, list):
                    print(f"  {k}: {len(v)} items")
                elif isinstance(v, dict):
                    print(f"  {k}: {json.dumps(v)}")
                else:
                    print(f"  {k}: {v}")
        else:
            print(f"✗ {args.command} failed: {result.get('error', 'unknown error')}")

    sys.exit(0 if result.get("ok") else result.get("error_code", 1))


if __name__ == "__main__":
    main()
