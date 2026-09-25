---
description: Bootstrap the orchestrator-runtime engine into a project directory — full state, slash commands, slash hooks
---

# /setup-project — Bootstrap the Orchestrator

Sets up a project directory with the full orchestrator engine: the 4-doc spine (CLAUDE.md, ARCHITECTURE.md, WORKFLOW.md, CONVENTIONS.md), the runtime scripts (burn queue, lease, heartbeat, watchdog, outbox, state machine, dependency audit, verdict conflict, DAG optimizer), the 20 slash commands renamed with the project prefix, the SessionStart hook, the Postgres / SQLite / file-based state, and the initial directory tree.

Idempotent — re-running on an already-bootstrapped project offers re-sync / upgrade / abort.

## Usage

```
/setup-project                          # set up in the current directory
/setup-project /path/to/project         # set up in a specific directory
/setup-project --re-sync                # force re-sync mode (refresh engine namespace only)
/setup-project --upgrade                # force upgrade mode (overwrite engine scripts with newer versions)
```

`$ARGUMENTS` is parsed for the path and the optional flags.

---

## Step 1 — Resolve target directory

Parse `$ARGUMENTS`:

- If empty → target = CWD (`$(pwd)`).
- If the first arg starts with `--` → treat it as a flag, target = CWD.
- Otherwise the first non-flag arg is the target. Verify it exists (`test -d`). If not, STOP with an error and tell the user the path doesn't exist.

Flags recognized: `--re-sync`, `--upgrade`. Both can appear alongside a path. Strip them from the path resolution.

## Step 2 — Pre-flight detection (parallel Bash)

Run these probes in parallel via a single Bash call:

```bash
TARGET="<resolved path>"
cd "$TARGET"
echo "TARGET=$TARGET"
echo "GIT=$(test -d .git && echo yes || echo no)"
echo "CLAUDE_MD=$(test -f CLAUDE.md && echo yes || echo no)"
echo "ORCH_DIR=$(test -d orchestrator && echo yes || echo no)"
echo "ENGINE_SCRIPTS=$(test -d orchestrator/scripts && echo yes || echo no)"
echo "SLASH_COMMANDS=$(ls .claude/commands/ 2>/dev/null | wc -l | tr -d ' ')"
echo "PSYCOPG2=$(python3 -c 'import importlib.util; print("yes" if importlib.util.find_spec("psycopg2") else "no")' 2>/dev/null || echo no)"
echo "PYTHON=$(python3 --version 2>&1 | awk '{print $2}')"
```

Capture the output. These values drive the next steps.

## Step 3 — Verify the orchestrator-runtime package is available

The engine source must be available before bootstrap. Run:

```bash
python3 -c "from orchestrator_runtime import cli; print('ok')" 2>/dev/null && echo "ENGINE_SOURCE=pip"
```

If that fails, try the vendored location:

```bash
test -d "$HOME/code/orchestrator-runtime/src/orchestrator_runtime" && echo "ENGINE_SOURCE=vendored:$HOME/code/orchestrator-runtime/src/orchestrator_runtime"
```

If neither works, ask the user via AskUserQuestion:

- "I can't find orchestrator-runtime. How should I proceed?"
  - Option A: "Install from pip — `pip install orchestrator-runtime`" (then re-probe)
  - Option B: "I'll give you a path to a vendored copy" → free-text → re-probe

## Step 4 — Pre-existing orchestrator? (skip if `--re-sync` or `--upgrade` is set)

If `ORCH_DIR=yes` AND `ENGINE_SCRIPTS=yes` AND the `--re-sync` / `--upgrade` flag was NOT set:

AskUserQuestion:
- "This project already has the orchestrator. What do you want to do?"
  - `re-sync` (recommended) — refresh only the engine namespace (`orchestrator/scripts/`, the global slash commands, `.claude/settings.local.json`). NEVER touches your project's source code, docs, or tickets.
  - `upgrade` — like re-sync, but also bumps the engine version metadata (use after the engine has new fixes).
  - `abort` — exit without touching anything.

## Step 5 — Gather project config (AskUserQuestion, max 4 questions at a time)

Defaults are inferred from path / file probes. Only ask for what's NOT auto-detectable.

**Round 1 — Project identity:**

1. **Project name** — text input. Default: basename of TARGET lowercased.
2. **Command prefix** — text input. Default: project name with dashes → underscores, max 12 chars. The prefix becomes `<prefix>-burn`, `<prefix>-work`, `<prefix>-discover`, etc.
3. **Multi-tenant?** — single-select. Default: yes if the project has customer data + an auth layer; otherwise no.
   - `yes` — full RLS setup (per-tenant FK + GUC `app.current_tenant` policy + multi-tenant non-negotiable slot).
   - `no` — single-tenant DB; tenant_id column optional.

**Round 2 — State infrastructure:**

4. **State store** — single-select. ALWAYS shown. Drives which backend the orchestrator's lease + outbox state lives in.
   - `postgres` (recommended if Postgres is reachable) — full Postgres-backed lease + outbox. Requires `psycopg2`.
   - `sqlite` — single-file DB at `orchestrator/state.db`. Good for local-first apps that already use SQLite (e.g. Tauri + better-sqlite3 projects).
   - `file` — no DB. Lease = file lock at `orchestrator/state/lease-<ticket_id>.json`. Outbox = append-only JSONL at `orchestrator/state/outbox.jsonl`. Single-machine only.
   - `skip` — no state layer. Orchestrator scripts still work (burn queue, dependency audit, regenerate metrics) but the lease and outbox are no-ops.

5. **Git repo init** — single-select. Only shown if `GIT=no`.
   - `init now` — `git init` + initial commit of the bootstrapped structure
   - `skip` — leave it; user will init themselves
6. **Initial state** — single-select.
   - `cold-start` — empty queue, no tickets. Use this if the project is brand new.
   - `discover-from-todos` — run the enhanced discovery (4 passes: TODOs, drift markers, README phases, TBD sections). Generate tickets automatically.
   - `import-existing` — user will drop ticket files into `tickets/` manually.

**Default inference for state-store:**
- `multi-tenant=yes` + Postgres reachable → `postgres`
- `multi-tenant=no` + Postgres reachable → `postgres` (still useful for lease/outbox)
- No Postgres + project has SQLite → `sqlite`
- No Postgres + no SQLite → `file`
- User explicitly says "skip" → `skip`

## Step 6 — Invoke the Python entry-point

The slash command delegates to `orchestrator-setup` (installed by `pip install orchestrator-runtime`). The CLI is argparse-based — testable and free of shell-escape hell:

```bash
orchestrator-setup init \
  --target "$TARGET" \
  --project-name "<name>" \
  --prefix "<prefix>" \
  --mode "$MODE" \
  --postgres-url "<url>" \
  --multi-tenant <yes|no> \
  --state-store <postgres|sqlite|file|skip> \
  --git-init <yes|no> \
  --initial-state <cold|discover|import> \
  --domain "<domain>" \
  --json
```

The `--json` flag tells the CLI to print a structured result. If `ok=false`, surface the error and STOP.

## Step 7 — Verify

```bash
orchestrator-setup doctor --target "$TARGET" --prefix "<prefix>" --state-store "<state-store>"
```

If any check fails, surface the failure. Do NOT mark the bootstrap complete.

## Step 8 — Suggest next action

Based on `initial-state`:

- `cold-start` → "Run `/<prefix>-fill-queue` to scan for TODOs in your code, or `/<prefix>-ticket` to file your first ticket."
- `discover-from-todos` → "Discovered N tickets. Top 3: [list]. Run `/<prefix>-work` to start the first one."
- `import-existing` → "Drop your ticket files into `orchestrator/tickets/`, then run `/<prefix>-burn --list-only` to see what's queued."

Then print the final summary:

```
✓ Orchestrator bootstrapped in <TARGET>
  Mode:           <install|re-sync|upgrade>
  Project:        <name>
  Prefix:         <prefix>
  Multi-tenant:   <yes|no>
  State store:    <postgres|sqlite|file|skip> (<status>)
  Slash commands: 20 (global — invoke /<prefix>-burn from any project)
  Engine source:  <pip|vendored>
  Tickets seeded: <N from discover | 0>

Next: <the suggested action above>
```

---

## Notes

- **Idempotency.** Re-running on a bootstrapped project defaults to abort unless `--re-sync` or `--upgrade` is passed. The re-sync mode only touches files under `orchestrator/scripts/`, the global slash commands, and `.claude/settings.local.json` — it NEVER overwrites `CLAUDE.md`, `tickets/`, or the user's source code.
- **State is sacred.** The script never deletes tickets. If `tickets/` is non-empty, it logs "Found N existing tickets" and skips the cold-start initialization.
- **Multi-tenant is one switch.** `--multi-tenant yes` enables RLS policies and adds a per-tenant non-negotiable. `--multi-tenant no` skips both. There's no half-state.
- **Engine source priority.** pip > vendored. The package is the only source — no project-specific fallback paths.
- **Domain → specialist prompts.** If the user named a domain, offer to scaffold the right specialist prompts in a follow-up step. For `general`, skip.
