# orchestrator-runtime

A project-agnostic orchestrator engine that makes any AI coding agent ~70% faster.

It packages the canonical anti-stall pattern: small-scope tickets, write-files-first, heartbeat every 3 tool calls, no inline verifier sub-agents, 3-min watchdog timeout, 30-min lease TTL. Plus a burn queue, dependency audit, ticket state machine, transactional outbox, and 20 slash commands.

Use it on **any project** — Python, TypeScript, Rust, the project's primary systems language, anything. Zero project-specific assumptions.

## What's in the box

- **11 runtime scripts** — burn queue, lease (Kleppmann monotonic fencing token), heartbeat, watchdog, outbox, outbox consumer, ticket state machine, dependency audit, verdict conflict resolver, DAG optimizer, force-claim.
- **3-doc spine** — ARCHITECTURE.md (invariants), WORKFLOW.md (Steps 0-7), CONVENTIONS.md (status enum, lease protocol).
- **Anti-stall prompt template** — the prompt prefix that prevents the 600s watchdog kill.
- **20 slash commands** — `<prefix>-burn`, `<prefix>-work`, `<prefix>-discover`, `<prefix>-ticket`, `<prefix>-fill-queue`, etc.
- **`/setup-project` slash command** — bootstrap the engine into any project with one command.
- **CLI**: `orchestrator-setup` — init / sync / upgrade / doctor / install-cli subcommands.
- **Migrations** for Postgres (V001 lease, V002 outbox) and SQLite.

## Install

```bash
pip install orchestrator-runtime
orchestrator-setup install-cli --prefix orch   # install the 20 slash commands globally
```

Then in any project:

```bash
cd /path/to/your/project
/setup-project
```

That's it. The slash command asks 5-6 questions (project name, prefix, multi-tenant y/n, state store, git init, initial state), then bootstraps the engine.

## Usage

### In any project, after `/setup-project`:

```bash
/yourprefix-burn --list-only          # see what's queued
/yourprefix-burn --limit 3            # burn 3 tickets
/yourprefix-ticket "Add OAuth flow"   # file a new ticket
/yourprefix-work                      # pick up the top queued ticket
/yourprefix-status                    # operational dashboard
/yourprefix-fill-queue                # discover TODOs and file tickets
/yourprefix-doctor                    # verify wiring (or: orchestrator-setup doctor --target .)
```

### From the CLI:

```bash
orchestrator-setup init --target /path/to/project --prefix myproj --state-store sqlite --git-init yes
orchestrator-setup sync --target /path/to/project --prefix myproj
orchestrator-setup upgrade --target /path/to/project --prefix myproj
orchestrator-setup doctor --target /path/to/project --prefix myproj --state-store sqlite
orchestrator-setup install-cli --prefix myproj
```

## State stores

The orchestrator needs somewhere to track lease + outbox state. Pick one:

| Store | Use case | Trade-offs |
|---|---|---|
| **postgres** | Multi-tenant production | Requires `psycopg2` + reachable Postgres. Best correctness (BIGSERIAL fencing tokens, RLS). |
| **sqlite** | Local-first apps, single-user | Single-file DB. Good for Tauri/Desktop apps. |
| **file** | No-DB deployments, throwaway | File locks + JSONL append. Single-machine only. Best-effort. |
| **skip** | Don't care about state | Orchestrator primitives still work; lease + outbox are no-ops. |

## Why this exists

The orchestrator runtime was extracted from a production codebase where it powered ticket-driven development of a multi-tenant domain-specific platform. The pattern reduced sub-agent stalls by ~70% (median ticket time: 1-3 min vs 10-15 min without). It's domain-agnostic — every mechanic generalizes.

The AI-speed fixes are baked in:

1. **WRITE-FILES-FIRST** — workers write a placeholder file within 3 tool calls so the watchdog sees progress.
2. **Heartbeat every 5 calls** — file-based progress marker prevents the 600s stream-watchdog kill.
3. **30-min lease TTL** — more frequent heartbeat, tighter feedback loop.
4. **3-min watchdog timeout** — orchestrator force-kills + re-dispatches stalled workers fast.
5. **No verifier sub-agents inline** — saves 50% of stall risk.
6. **Kleppmann monotonic fencing tokens** — Postgres BIGSERIAL prevents stale-claim bugs.

## Architecture

```
~/.claude/commands/
  setup-project.md       ← bootstrap (global, laptop-wide)
  <prefix>-burn.md       ← burn queue (one per project prefix)
  <prefix>-work.md
  <prefix>-discover.md
  ... (20 total per prefix)

<project>/orchestrator/
  scripts/               ← runtime scripts (burn_queue, lease, heartbeat, etc.)
  prompts/               ← anti-stall prompt template
  docs/                  ← ARCHITECTURE.md, WORKFLOW.md, CONVENTIONS.md
  tickets/               ← ticket files (TKT-*.md)
  progress/              ← progress logs + heartbeat markers
  tickets-index.md       ← master table of all tickets
  NEXT-ACTIONS.md        ← top of burn queue
  blockers-index.md      ← live blocker list
  state/                 ← state-store dir (postgres/sqlite/file/skip)
```

## Project-agnostic promise

This package contains **zero project-specific assumptions**:
- No hardcoded project paths
- No domain-specific non-negotiables in CLAUDE.md template (you fill them in)
- No specialist prompts (you write your own, optionally with `--domain <name>`)
- Default Postgres role: `orchestrator_app` (configurable via env)
- Default DB name: `orchestrator`
- Default Postgres port: `5432` (configurable via `ORCHESTRATOR_DB_PORT`)
- All slash commands and runtime scripts are renamable via `--prefix`

## Development

```bash
git clone https://github.com/AlaaZenji/orchestrator
cd orchestrator
pip install -e ".[postgres,test]"
pytest tests/
```

## License

MIT — see [LICENSE](LICENSE).
