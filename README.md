# orchestrator-runtime

A **minimal, project-agnostic** orchestrator engine. Throw it on any folder.

It packages the canonical anti-stall pattern: small-scope tickets, write-files-first, heartbeat every 3 tool calls, no inline verifier sub-agents, 3-min watchdog timeout, 30-min lease TTL. Plus a burn queue, dependency audit, ticket state machine, and transactional outbox.

Use it on **any project** — Python, TypeScript, Rust, Go, anything.

## What's in the box

- **11 runtime scripts** — burn queue, lease (Kleppmann monotonic fencing token), heartbeat, watchdog, outbox, outbox consumer, ticket state machine, dependency audit, verdict conflict resolver, DAG optimizer, force-claim.
- **3-doc spine** — ARCHITECTURE.md (invariants), WORKFLOW.md (Steps 0-N), CONVENTIONS.md (status enum, lease protocol).
- **Anti-stall prompt template** — the prompt prefix that prevents the 600s watchdog kill.
- **`/setup-project` slash command** — bootstrap the engine into any project.
- **CLI**: `orchestrator-setup` — init / sync / upgrade / doctor / install-cli subcommands.
- **Migrations** for Postgres (V001 lease, V002 outbox) and SQLite.

## What's intentionally NOT included

- Per-prefix slash commands (`<prefix>-burn`, `<prefix>-ticket`, etc.) — these were removed for true project-agnosticism. Write your own domain-specific commands after bootstrap. The CLI exposes the runtime via `python3 orchestrator/scripts/burn_queue.py` etc.
- Specialist prompts (Hospital Ops, Regulatory, KYC, etc.) — write your own for your domain.
- Domain-specific non-negotiables in CLAUDE.md — the template gives you generic placeholders.

## Install

```bash
pip install orchestrator-runtime
orchestrator-setup install-cli    # installs /setup-project globally
```

Then in any project:

```bash
cd /path/to/your/project
/setup-project
```

That's it. The slash command asks 5-6 questions (project name, prefix, multi-tenant y/n, state store, git init, initial state), then bootstraps the engine.

## Usage

### CLI:

```bash
orchestrator-setup init --target /path/to/project --prefix myproj --state-store sqlite --git-init yes
orchestrator-setup sync --target /path/to/project --prefix myproj
orchestrator-setup upgrade --target /path/to/project --prefix myproj
orchestrator-setup doctor --target /path/to/project --prefix myproj --state-store sqlite
orchestrator-setup install-cli
```

### Runtime scripts (after bootstrap):

```bash
python3 orchestrator/scripts/burn_queue.py --list-only
python3 orchestrator/scripts/dependency_audit.py
python3 orchestrator/scripts/lease.py claim --ticket-id TKT-NNN --holder-id me
```

## State stores

| Store | Use case | Trade-offs |
|---|---|---|
| **postgres** | Multi-tenant production | Requires `psycopg2` + reachable Postgres. BIGSERIAL fencing tokens + RLS. |
| **sqlite** | Local-first apps, single-user | Single-file DB at `orchestrator/state.db`. |
| **file** | No-DB deployments | File locks + JSONL append. Single-machine only. |
| **skip** | Don't care about state | Orchestrator primitives still work; lease + outbox are no-ops. |

Configurable via env vars:

```
ORCHESTRATOR_DB_HOST  (default localhost)
ORCHESTRATOR_DB_PORT  (default 5432)
ORCHESTRATOR_DB_NAME  (default orchestrator)
ORCHESTRATOR_DB_USER  (default orchestrator_app)
ORCHESTRATOR_DB_PASSWORD  (default empty)
ORCHESTRATOR_DB_ROLE  (default orchestrator_app — role to grant in migrations)
```

## Anti-stall pattern (the AI-speed fixes)

1. **WRITE-FILES-FIRST** — workers write a placeholder file within 3 tool calls so the watchdog sees progress.
2. **Heartbeat every 5 calls** — file-based progress marker prevents the 600s stream-watchdog kill.
3. **30-min lease TTL** — more frequent heartbeat, tighter feedback loop.
4. **3-min watchdog timeout** — orchestrator force-kills + re-dispatches stalled workers fast.
5. **No verifier sub-agents inline** — saves 50% of stall risk.
6. **Kleppmann monotonic fencing tokens** — Postgres BIGSERIAL prevents stale-claim bugs.

## Architecture

```
~/.claude/commands/
  setup-project.md       ← global, laptop-wide

<project>/orchestrator/
  scripts/               ← 11 runtime scripts (burn_queue, lease, heartbeat, ...)
  prompts/               ← anti-stall prompt template
  docs/                  ← ARCHITECTURE.md, WORKFLOW.md, CONVENTIONS.md
  tickets/               ← ticket files (TKT-*.md)
  progress/              ← progress logs + heartbeat markers
  tickets-index.md       ← master table of all tickets
  NEXT-ACTIONS.md        ← top of burn queue
  blockers-index.md      ← live blocker list
  state/                 ← state-store dir (postgres/sqlite/file/skip)
  state.db               ← (only if state-store=sqlite)
```

## Project-agnostic promise

This package contains **zero project-specific assumptions**:
- No hardcoded project paths
- No domain-specific non-negotiables in CLAUDE.md template
- No specialist prompts
- No per-prefix slash commands
- Default Postgres role: `orchestrator_app` (env-configurable)
- Default DB name: `orchestrator`
- Default Postgres port: `5432` (env-configurable)

Throw it on a new folder; it bootstraps.

## License

MIT — see [LICENSE](LICENSE).
