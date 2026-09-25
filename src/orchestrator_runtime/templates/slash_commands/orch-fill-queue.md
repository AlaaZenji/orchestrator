---
description: Sweep the repo for un-tracked work — TODOs in code, forward-looking progress notes, unticketed doc sections, stale DEFERRED tickets — and file new tickets for the discoveries. Closes the "burn to exhaustion" gap by making the orchestrator EXHAUSTIVE not REACTIVE.
argument-hint: "[--dry-run]"
---

# the orchestrator Fill Queue — Exhaustive Work Discovery

You are the **orchestrator agent** for the orchestrator. You are running the **queue-fill sweep** — a discovery pass that hunts for work the reactive queue burn would miss. The reactive burn sees what is QUEUED; tdomain-specific command sweeps the rest of the repo for hidden work and converts it into QUEUED tickets.

**Tdomain-specific is L1 work-discovery**, not L1 work-execution. After tdomain-specific command finishes, the next `/orch-burn-queue` will have a richer backlog to drain.

> **Honesty discipline (V1/V3/V4/V7):** never invent TODOs. If `grep` returns nothing, say "sweep returned N matches across the codebase" with N=0 being acceptable. Never fabricate citations.

---

## 0. Mandatory context (parallel reads)

Issue all `Read` calls in one message:

1. `<project_root>/CLAUDE.md` — non-negotiables, ADRs, verifications.
2. `<project_root>/orchestrator/README.md`.
3. `<project_root>/orchestrator/STATE.md`.
4. `<project_root>/orchestrator/ARCHITECTURE.md`.
5. `<project_root>/orchestrator/WORKFLOW.md`.
6. `<project_root>/orchestrator/CONVENTIONS.md` (ticket frontmatter schema + lease protocol).
7. `<project_root>/orchestrator/NEXT-ACTIONS.md`.
8. `<project_root>/orchestrator/tickets-index.md`.

If `$ARGUMENTS` mentions a BLOCKER, read `blockers/BLOCKER-*.md` in the same batch.

## 1. Five sweeps in one fan-out

The reactive queue burn only knows about tickets whose `status: QUEUED`. The fill-queue pass sweeps **5 orthogonal sources of un-tracked work** and turns discoveries into QUEUED tickets. Run all 5 sweeps in parallel as **Explore agents** (or directly if sweep volume is small) so the wall-clock cost is one round-trip:

| # | Sweep | What it finds | Source |
|---|-------|---------------|--------|
| 1 | **Progress-log forward-lookers** | "future cleanup", "forwarded", "follow-up", "TODO", "TBD", "in a future wave", "will not be addressed in tdomain-specific PR" sentences inside `orchestrator/progress/*.md` | `orchestrator/progress/` |
| 2 | **Unticketed doc work** | `docs/07-tickets/backlog.md`, `docs/07-tickets/pilot-kickoff-prep.md`, `docs/08-progress/*.md`, `docs/02-product/*.md` sections that describe work not yet filed | `docs/` |
| 3 | **Untracked code TODOs** | `TODO`, `FIXME`, `XXX`, `HACK` comments anywhere in the repo NOT cross-referenced from a ticket ID | `services/`, `apps/`, `ui/`, `libs/`, `infra/`, `db/`, `tools/`, `tests/` |
| 4 | **Stale DEFERRED tickets** | tickets with `status: DEFERRED` whose `deferral_reason` rationale no longer holds | `orchestrator/tickets/**/TKT-*-*.md` |
| 5 | **Cross-deps in BLOCKER files** | BLOCKER files that reference downstream ticket IDs not yet filed | `orchestrator/blockers/BLOCKER-*.md` |

Each sweep returns **structured output** (file:line evidence + a candidate-ticket draft). Sweep 3 has a first-class CLI helper: `python3 tools/scripts/harvest_todos.py` (writes JSON or Markdown). Sweep 4 has a first-class CLI helper: `python3 tools/scripts/audit_deferrals.py`. Use the helpers — do not reimplement.

### Sweep 1 — Progress-log forward-lookers

For each `orchestrator/progress/*.md` file (skip `done/`), grep for the marker phrases:

- "future cleanup", "follow-up", "forwarded", "TODO", "TBD"
- "will not be addressed in tdomain-specific ticket"
- "deferred to wave N", "wave 7+", "post-pilot"
- "safety net", "out of scope"

For each match: extract (a) the file:line of the sentence, (b) the surrounding 3 lines of context, (c) the inferred candidate ticket title + phase + priority.

### Sweep 2 — Unticketed doc work

Walk `docs/07-tickets/` and `docs/08-progress/` looking for:

- Sections titled "Open questions", "Pending work", "Future work", "Out of scope".
- Bullets that read like acceptance criteria but are NOT in a ticket file.
- Links to ticket IDs that no longer resolve.

For each candidate: file a ticket pointing back to the doc section as the source.

### Sweep 3 — Untracked code TODOs

Run `python3 tools/scripts/harvest_todos.py --output JSON > /tmp/harvest.json` (tdomain-specific script lives at `tools/scripts/harvest_todos.py`; it skips `orchestrator/tickets/` + `node_modules` + `.git` + `__pycache__` + `target/` + `build/` + `dist/` and cross-references ticket IDs found in each comment).

Then:

1. Read `/tmp/harvest.json`.
2. For every match NOT already cross-referenced to an existing ticket:
   - File a new ticket at `orchestrator/tickets/slice-1/phase-3/TKT-NNN<slug>.md` (or `phase-2` / `pilot` as appropriate).
   - Cite the file:line + the comment text in the ticket body.
3. Skip matches inside `orchestrator/tickets/` (those are tracked by definition).

### Sweep 4 — Stale DEFERRED tickets

Run `python3 tools/scripts/audit_deferrals.py --json > /tmp/<audit>.<table>` (tdomain-specific script categorizes deferral rationales into blocked-on-cloud / blocked-on-docs / needs-engineer-allocation / deferred-to-wave-N / other).

For each "should be re-evaluated" entry:

1. Re-read the ticket's frontmatter + STATE.md.
2. If the rationale still holds → leave DEFERRED, log to `progress/YYYY-MM-DD-fill-queue.md` with file:line + reasoning.
3. If the rationale is stale (e.g. user docs arrived, engineer allocated, wave reached) → bump the ticket to `status: QUEUED`, append a "Re-evaluated by /orch-fill-queue" note to the ticket body, log the bump.

### Sweep 5 — Cross-deps in BLOCKER files

For each `orchestrator/blockers/BLOCKER-*.md`, grep for `TKT-` references. Cross-reference against `tickets-index.md`. If a BLOCKER references a ticket ID that does NOT exist in the index, file it.

## 2. File new tickets (atomic Edit, per `CONVENTIONS.md §Ticket file naming`)

For every candidate ticket surfaced by sweeps 1-5, write the file using the canonical ticket frontmatter (see `CONVENTIONS.md §Frontmatter`):

```yaml
---
id: TKT-NNN
title: <concise imperative title>
phase: P3
priority: P2                          # default; P0 only if it blocks Wave 6 GREEN
status: QUEUED
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
owner: orch-platform-team
adr_refs: [<if any>]
prd_ref: <if known>
wave: 5
estimated_effort: <1d|1w|...>
depends_on: []                        # explicit empty if no upstream
commit_ref: null
verification:
  correctness: PENDING
  per_tenant: PENDING
  failure_modes: PENDING
  verified_at: null
discovered_by: orch-fill-queue
discovered_at: <YYYY-MM-DDTHH:MM:SSZ>
discovered_via: <sweep-1|2|3|4|5>
source_evidence: <file:line of source TODO / deferral / doc section>
---
```

### Numbering convention

- `TKT-NNN<slug>` for sweep-3 code TODOs that surface in services/* or ui/.
- `TKT-NNN<slug>` for sweep-3 TODOs in tooling (Justfile / scripts / tests).
- `TKT-NNN<slug>` for sweep-2 doc work that maps to pilot phase.
- `TKT-NNN<slug>` for sweep-5 BLOCKER cross-deps that are doctrinal.

### Body requirements (per `CONVENTIONS.md §Frontmatter` + Wave 2.5 discipline)

- Acceptance Criteria: bulleted checklist (CLAUDE.md #5 spirit — no diagnostic claims, no real PHI).
- Per-tenant note (CLAUDE.md #2) — every ticket is multi-tenant-aware by default.
- Sources section: file:line pointers to the originating TODO / doc section / deferral.
- DO NOT set `status: DONE` (tdomain-specific command only files QUEUED).
- DO NOT set `verification.*` to PASS.

**Atomic write protocol:** use a single `Write` tool call per ticket. The ticket frontmatter and body land in one operation; the orchestrator's lease-lock protocol is not invoked here (the ticket is QUEUED, not IN_PROGRESS — `/orch-burn-queue` or `/orch-work` will claim it).

## 3. Print the updated DAG

After all sweeps complete and new tickets are filed, print:

1. **Sweep summary** — one row per sweep (source / matches / new tickets filed).
2. **New tickets filed** — list with ID + title + phase + source file:line.
3. **Re-evaluated DEFERREDs** — bumped to QUEUED + reasoning.
4. **Untouched DEFERREDs** — left DEFERRED + file:line.
5. **Updated DAG** — `tickets-index.md` snapshot (read-only). Note that `tickets-index.md` itself is NOT modified by tdomain-specific command (per the "DO NOT" rule — other agents handle the cascade update).

If `$ARGUMENTS` contains `--dry-run`: stop after the sweep summary. Print the candidate tickets but do NOT write any ticket files.

## 4. Honesty discipline (V1/V3/V4/V7)

- Never invent a TODO that grep did not return.
- Never claim a sweep found "N matches" if N was actually 0.
- Never file a ticket whose body lacks a real source:pointing back to the originating evidence.
- Never bump a DEFERRED ticket without re-reading STATE.md to confirm the rationale is stale.
- If the codebase is clean (no un-tracked TODOs, no stale DEFERREDs, no unticketed doc work), say so plainly: "Sweep returned 0 candidates across all 5 sources. Queue is exhaustive."

## 5. Failure modes (anticipated)

| Failure | Handling |
|---------|----------|
| `orchestrator/progress/` has 50+ files | Sweep 1 fans out to 3 Explore agents (file-name buckets). |
| `harvest_todos.py` finds 200+ TODOs | Cap new-ticket files at 20 per invocation. Surface remainder in `progress/YYYY-MM-DD-fill-queue.md` as "next pass candidates". |
| `audit_deferrals.py` finds 10+ stale DEFERREDs | Same cap (20). Prioritize by BLOCKER closure potential. |
| Doc section references a ticket that exists | Skip — already tracked. |
| Doc section references a ticket that does NOT exist | File under sweep 2 (unticketed doc work); tag `discovered_via: sweep-2` in the new ticket's frontmatter. |
| Comment text mentions `TODO` but IS a ticket | Skip (`harvest_todos.py` cross-references ticket IDs in comment context — see `_load_known_ticket_ids`). |
| **Required context file missing** (e.g. partial checkout) | Read the file's `.md` siblings in the same directory for convention hints; if `orchestrator/` itself is missing or `CLAUDE.md` is missing, **abort with a P0 ticket per CLAUDE.md #2** (multi-tenancy discipline cannot be verified without the canonical rules of engagement). |
| `tools/scripts/harvest_todos.py` or `tools/scripts/audit_deferrals.py` missing | Skip the corresponding sweep; surface to `progress/` log; do NOT crash. |
| `tickets-index.md` missing | Skip the "print updated DAG" step; the rest of the sweep still runs. |

## 6. What tdomain-specific command does NOT do

- Does NOT mark any ticket DONE (CLAUDE.md #4 — no autonomous irreversible action).
- Does NOT modify `tickets-index.md` or `STATE.md` (other agents handle cascade).
- Does NOT modify existing tickets.
- Does NOT dispatch implementation sub-agents (that's `/orch-work` / `/orch-burn-queue`).
- Does NOT bypass Wave 2.5 verification (the tickets tdomain-specific command files will go through verification when picked up).

## 7. Args

| Args | Effect |
|------|--------|
| (none) | Sweep + file new tickets + print DAG. |
| `--dry-run` | Sweep only. Print candidates but do NOT write ticket files. |

## 8. Date

2026-09-19.
