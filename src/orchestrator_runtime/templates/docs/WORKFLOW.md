# Workflow — How to Pick Up and Update a Ticket

> Every agent follows this workflow when working on a ticket. Domain-agnostic.

---

## Step 0 — Pre-flight resource probe (mandatory before any sub-agent dispatch)

```bash
echo "cpu=$(sysctl -n hw.ncpu) mem_gb=$(sysctl -n hw.memsize | awk '{print int($1/1024/1024/1024)}') free_pages=$(vm_stat | awk '/Pages free/ {print $3}' | tr -d '.')"
```

Use the result to set your **sub-agent concurrency cap**:

| Sub-agent type | RAM/agent | Safe concurrent cap |
|---|---|---|
| `Explore` (read-only) | ~50 MB | 6 |
| `general-purpose` (file edits) | ~200 MB | 4 |
| `Plan` (decomposition) | ~500 MB | 1 |
| `Workflow` agents | varies | 10 (tool's hard cap) |

**Hard rules:**

- Two sub-agents editing the **same file** must NOT run in parallel — race condition.
- Pass `schema:` (JSON Schema) to any sub-agent returning structured output.
- Never recursive fan-out (depth cap = 2).

---

## Step 1 — Read context (mandatory, parallel)

Issue all `Read` calls in one message. The mandatory set:

1. `STATE.md` — where we are.
2. `ARCHITECTURE.md` — what you cannot violate.
3. `CONVENTIONS.md` — naming, status, commit format, sub-agent return schemas.
4. The ticket file itself in `tickets/`.
5. If the ticket references a BLOCKER, `blockers/BLOCKER-*.md`.

Time budget: 5 minutes max.

---

## Step 2 — Pick up the ticket

If `$ARGUMENTS` is given, route to that specific ticket file. Otherwise, take the top `QUEUED` ticket from `NEXT-ACTIONS.md` (skip if held by another agent).

### Step 2.1 — Atomic lease claim

The orchestrator prevents two agents working the same ticket with a lease-based lock. The lease lives in Postgres (or SQLite / file / skip per project choice). The bigserial `fencing_token` enforces monotonic ordering (Kleppmann). Read the full protocol in `CONVENTIONS.md §Ticket lease lock`.

**Availability decision tree:**

```
status == QUEUED                  → AVAILABLE. Run claim() → fresh fencing_token
status == IN_PROGRESS             → check lease_expires_at; if < now → STALE, reap + claim
                                   else → HELD, skip
status == DONE / CANCELLED        → unavailable, skip
status == BLOCKED                 → read BLOCKER file first; don't claim
status == PARTIAL                 → check lease_expires_at; reap if stale; else held, skip
```

### Heartbeat

If a ticket takes longer than the lease TTL, the orchestrator must heartbeat — extend `lease_expires_at` and rotate `fencing_token`. Do this on every commit, or every 10 minutes, whichever is sooner.

---

## Step 3 — Discovery (parallelization: sectioning)

For non-trivial tickets (multi-file change, AC > 3 bullets), do NOT read every file yourself. Section the investigation across up to 6 Explore agents in parallel, each scoped to one concern:

| Concern | Question it answers |
|---|---|
| Code reality | Read every file the ticket says to change + every test. Return file:line map. |
| Spec & docs | Read the AC, the ADR(s) referenced, the verification dossier. Return constraints. |
| Adjacent risk | Read callers of any function you'll change. Return downstream impact. |
| Per-tenant boundary | Find every `tenant_id` reference in scope; flag cross-tenant risk. |
| Test surface | Find existing test patterns + missing test surface for this AC. |
| Prior art | Grep `progress/`, git log for similar past tickets. |

For trivial tickets (1-file change, AC ≤ 3 bullets), skip the fan-out.

---

## Step 4 — Implementation (orchestrator-workers)

Decompose with one `Plan` agent (sequencing + risk register). Dispatch up to 4 `general-purpose` workers in parallel for independent edits. Each worker gets:

- Exact file path(s) + line numbers.
- Exact AC bullets it owns.
- The discovery findings it needs.
- A schema for its return.

If two workers need to edit the same file: sequence them, don't parallelize.

If you discover new work:
- Within the ticket's AC → fold it in.
- Adjacent to AC → update the ticket's "Notes / discoveries" section.
- New → file a new ticket in `tickets/`.

If you discover an invariant violation: STOP → §Step 7.

---

## Step 5 — Verify (parallelization: voting)

Dispatch 3-6 verifier agents in parallel, each with a distinct adversarial lens. Self-attestation is forbidden.

| Lens | Question |
|---|---|
| Correctness | Does the change satisfy every AC bullet, with file:line evidence? |
| Per-tenant | Does it preserve tenant discipline at every boundary? |
| Failure modes | What happens on empty input, timeout, retry, partial outage? |
| Reversibility (P0/P1) | Blast radius estimate. Clean revert path? |
| Security (P0/P1) | SQLi / XSS / secrets / authz bypass / SSRF / log injection? |
| Performance (P0/P1) | O(N^2) loops? Missing DB index? N+1 queries? |

Loop-until-PASS: if any verifier returns FAIL, re-dispatch the implementation worker with that verifier's `fix_suggestion`, then re-run all verifiers. Retry cap: 3. Exceeding the cap → auto-pause trigger.

---

## Step 6 — Status + cascade update

The cascade touches multiple files (ticket frontmatter → `tickets-index.md` → progress log → `STATE.md` → `blockers-index.md` → commit). Use the transactional outbox pattern (see `outbox.py`):

1. Enqueue all cascade actions to `orchestrator.outbox` in a single transaction.
2. Run the outbox consumer (applies actions in order with idempotency).
3. Verify cascade consistency.
4. Mark LANDED when the change is in a durable external record (git commit, user ratification, cloud deploy).
5. Regenerate derived state (metrics, NEXT-ACTIONS).
6. Write checkpoint so the next burn can resume after a crash.

---

## Step 7 — Auto-pause triggers

| Trigger | Action |
|---|---|
| CLAUDE.md violation detected | STOP. Raise P0 BLOCKER. Notify user. |
| Wrong foundational decision | STOP. Surface the conflict. |
| Resource ceiling hit (CPU/RAM) | STOP. Lower concurrency cap. |
| Verification FAIL after 3 retries | STOP. Surface failing perspective + fix_suggestion. |
| Blocker is user-team-owned | STOP. Tell user. Don't invent workaround. |

---

## When in doubt

- Re-read the relevant ADR.
- Read the ticket's prior-art section.
- File a BLOCKER, don't expand scope.
- Re-check the cap table in §0 before adding more sub-agents.

## Forbidden shortcuts

- Don't skip multi-perspective verification.
- Don't mark DONE without `commit_ref` + `verification.evidence` + all perspectives PASS.
- Don't expand scope silently — file a new ticket.
- Don't fan out sub-agents that edit the same file in parallel.
- Don't recursive-fan-out.
- Don't skip the resource probe before dispatching.
- Don't pass whole files to sub-agents — pass paths + snippets.
