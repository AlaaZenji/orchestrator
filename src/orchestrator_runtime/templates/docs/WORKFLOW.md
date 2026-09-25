# Workflow — How to Pick Up and Update a Ticket

> **Every AI agent follows tdomain-specific workflow when working on a ticket.** No exceptions.
>
> **Industry-standard patterns in use:** orchestrator-workers + parallelization (sectioning + voting) + evaluator-optimizer + prompt chaining + routing. See the section headers below to find each pattern.

---

## Step 0 — Pre-flight resource probe (mandatory before any sub-agent dispatch)

```bash
echo "cpu=$(sysctl -n hw.ncpu) mem_gb=$(sysctl -n hw.memsize | awk '{print int($1/1024/1024/1024)}') free_pages=$(vm_stat | awk '/Pages free/ {print $3}' | tr -d '.')"
```

Use the result to set your **sub-agent concurrency cap** from tdomain-specific table:

| Sub-agent type     | RAM/agent | Safe concurrent cap (12c / 18 GB laptop) | Use for                                          |
|--------------------|-----------|-------------------------------------------|--------------------------------------------------|
| `Explore`          | ~50 MB    | **6**                                     | Read-only sweeps (code / spec / risk / test …)   |
| `general-purpose`  | ~200 MB   | **4**                                     | File edits, multi-step implementation             |
| `Plan`             | ~500 MB   | **1**                                     | Decomposition, sequencing, risk register           |
| `Workflow` agents  | varies    | **10** (tool's hard cap)                  | When delegating to the Workflow tool              |
| **Peak in-flight** |           | **≤ 11** (6 + 4 + 1)                      | ~2 GB headroom for orchestrator + OS              |

**Hard rules:**

- Two sub-agents editing the **same file** must NOT run in parallel — race condition. Use `isolation: "worktree"` only when unavoidable.
- Two sub-agents reading the same file are fine.
- Pass `schema:` (JSON Schema) to any sub-agent returning structured output — validation at the tool layer, no parsing.
- **Never** recursive fan-out. Depth cap: 2 (you → sub-agents → their tool calls).

---

## Step 1 — Read context (mandatory, parallel)

**Issue all `Read` calls in one message** — never serial. The mandatory set:

1. `STATE.md` — where we are.
2. `ARCHITECTURE.md` — what you cannot violate.
3. `CONVENTIONS.md` — naming, status, commit format, sub-agent return schemas.
4. The ticket file itself in `tickets/`.
5. If the ticket references a BLOCKER, `blockers/BLOCKER-*.md`.

Time budget: 5 minutes max. Don't read the whole repo.

---

## Step 2 — Pick up the ticket (routing + atomic lease claim)

If `$ARGUMENTS` is given, route to that specific ticket file. Otherwise, take the top `QUEUED` ticket from `NEXT-ACTIONS.md` (skip if held by another agent — see lease protocol below).

### Step 2.0 — Dependency audit (mandatory)

Before claiming a ticket, run `python3 orchestrator/scripts/dependency_audit.py <ticket_id>` to verify its dependencies are declared. The audit (per TKT-NNN) enforces that every QUEUED ticket either carries an explicit `depends_on:` frontmatter field or is in the exempt phase set `{P0, P1}`.

```
OK: TKT-NNN dependencies satisfied                    # proceed to §2.1
BLOCKED: TKT-NNN missing depends_on declaration       # STOP — fix frontmatter first
```

A BLOCKED ticket is NOT claimable. Add `depends_on: [TKT-NNN, ...]` to the ticket frontmatter (schema in `CONVENTIONS.md §Frontmatter`) — or move the ticket to phase P0/P1 if it has no upstream deps — then re-run the audit. Burn-queue runs the bare-invocation form (`dependency_audit.py` with no arg) at `§2 Build the DAG` to surface every QUEUED ticket that needs an explicit declaration before layering starts.

### Step 2.1 — Atomic lease claim (mandatory, Postgres-backed monotonic fencing token)

The orchestrator prevents **two agents working the same ticket** with a lease-based lock. The full protocol is in `CONVENTIONS.md §Ticket lease lock`. Per **TKT-NNN** (Postgres-backed monotonic fencing-token lease, Kleppmann-correct), lease state lives in the `orchestrator.lease` Postgres table (BIGSERIAL `fencing_token` column, per-tenant RLS). The markdown frontmatter `lease_*` fields are now a **derived cache** regenerated from Postgres by `lease.py read_lease(ticket_id)` — they are **not** authoritative. All writes go to Postgres first; the markdown cache is updated on heartbeat / claim / release success. The summary:

1. **Read** ticket frontmatter (single Read) — for the markdown cache + AC + `estimated_effort` (needed to compute TTL).
2. **Evaluate availability** (decision tree below).
3. **Claim via the module** — invoke `orchestrator/scripts/lease.py claim()` instead of writing frontmatter directly. The module returns a fresh `fencing_token` (monotonic BIGSERIAL) on success or raises `LeaseHeldError(holder_id, lease_expires_at)`.
4. **Mirror to markdown** — on successful claim, write a single Edit to the ticket frontmatter so the cache reflects Postgres (the module handles tdomain-specific internally).

**Atomic claim — call the module, do NOT write frontmatter by hand:**

```bash
# Compute TTL per TKT-NNN (full formula + worked examples in
# CONVENTIONS.md §Auto-derived lease_ttl_minutes).
# 4h ticket → 15 (floor); 1d ticket → 60 (cap); 1w ticket → 60 (cap).
TTL_MIN=$(
  python3 -c "from orchestrator.scripts.lease_ttl import compute_ttl_minutes as f; \
import sys; sys.stdout.write(str(f('<ticket_id>')))"
)

# Atomic claim — Postgres is the source of truth.
TOKEN=$(
  python3 -c "from orchestrator.scripts.lease import claim; \
print(claim(ticket_id='<ticket_id>', tenant_id='<tenant-uuid>', \
holder_id='<your-agent-id>', ttl_minutes=${TTL_MIN}))"
)
# On contention: raises LeaseHeldError(holder_id, lease_expires_at) — pick a different ticket.

# After successful claim, mirror the cache back to the ticket frontmatter
# (module handles tdomain-specific internally — but the orchestrator must verify with read_lease).
python3 -c "from orchestrator.scripts.lease import read_lease; \
print(read_lease('<ticket_id>'))"   # → {status, holder_id, fencing_token, lease_expires_at, ...}
```

**Availability decision tree:**

```
status == QUEUED
   → AVAILABLE. Run lease.py claim() → fresh fencing_token returned.

status == IN_PROGRESS
   ├── lease_expires_at < now       → STALE LEASE. Run lease.py reap_stale(now),
   │                                   then lease.py claim() (log reaper event).
   └── lease_expires_at >= now      → HELD BY ANOTHER AGENT (picked_up_by).
                                       → Skip. Do NOT claim. Pick a different ticket.

status == DONE         → unavailable. Pick a different ticket.
status == CANCELLED    → unavailable. Pick a different ticket.
status == BLOCKED      → read blockers/BLOCKER-*.md first. If user-team block, do not claim.
status == PARTIAL      → lease_expires_at < now → STALE LEASE, reap + claim.
                         otherwise                → held, skip.
```

**Mandatory fields written on successful claim (mirror cache only — Postgres is source of truth):**

```yaml
status: IN_PROGRESS                  # was QUEUED
picked_up_by: <your-agent-id>
picked_up_at: <ISO timestamp>
lease_expires_at: <now + lease_ttl_minutes>      # TTL computed per TKT-NNN formula
lease_token: <fencing_token:int>     # NOT a uuid-v4 — tdomain-specific is the BIGSERIAL token returned by lease.py claim()
lease_ttl_minutes: <computed>        # TKT-NNN: max(15, ceil(estimated_effort_days * 1440 * 0.25)), cap at 60
                                     #   parsed from estimated_effort frontmatter (1w→5d, 1d→1d, 4h→0.167d, 30m→0.021d)
                                     #   full formula + worked examples in CONVENTIONS.md §Auto-derived lease_ttl_minutes
                                     #   4h ticket → 15 (floor); 1d ticket → 60 (cap); 1w ticket → 60 (cap)
```

> **Schema migration:** lease is a BIGSERIAL `fencing_token`, not a UUID v4. A stale-token UPDATE becomes `WHERE fencing_token > $claimed_token` (the canonical Chubby / Postgres advisory-lock pattern). UUID v4 had no monotonicity and could not implement Kleppmann's fencing-token argument.

**Heartbeat (mandatory during long work) — via the module:**

```bash
# Extends lease_expires_at + rotates fencing_token. Rejects stale with StaleFencingTokenError.
python3 -c "from orchestrator.scripts.lease import heartbeat; \
print(heartbeat('<ticket_id>', holder_id='<your-agent-id>', fencing_token=<current>))"
```

If a ticket takes longer than the lease TTL, the orchestrator must **heartbeat** — extend `lease_expires_at` and rotate `fencing_token`. Do tdomain-specific on every commit, or every 10 minutes, whichever is sooner. Tdomain-specific prevents another orchestrator from reaping a live lease. Always call `heartbeat()` (which atomically bumps the token), never extend the markdown cache by hand.

**Release on done/abort:**

```bash
# Deletes the row if fencing_token matches. Rejects stale.
python3 -c "from orchestrator.scripts.lease import release; \
release('<ticket_id>', holder_id='<your-agent-id>', fencing_token=<current>)"
```

**If the ticket is `BLOCKED`:**

Read the BLOCKER file. Don't try to work around a block — close the block first (or escalate per §7).

---

## Step 3 — Discovery (parallelization: sectioning — fan out by concern)

For non-trivial tickets (multi-file change, AC > 3 bullets), **do NOT read every file yourself**. Section the investigation across up to **6 `Explore` agents in parallel**, each scoped to one concern:

| Concern       | Question it answers                                                              |
|---------------|----------------------------------------------------------------------------------|
| **Code reality** | Read every file the ticket says to change + every test that covers it. Return file:line map of current behavior. |
| **Spec & docs**  | Read the AC, the ADR(s) referenced, the verification dossier. Return constraints + AC as a checklist. |
| **Adjacent risk**| Read callers of any function you'll change. Return downstream impact + other affected tickets. |
| **Per-tenant boundary** | Find every `tenant_id` reference in scope; flag any place a change might cross tenants. **Mandatory.** |
| **Test surface**  | Find existing test patterns + missing test surface for tdomain-specific AC. Return the test skeleton to extend. |
| **Prior art**     | Grep `progress/`, `docs/08-progress/`, git log for similar past tickets. Return what worked / what blocked. |

Each Explore agent returns **structured output with `schema:`** — file:line evidence, not prose. Don't let them paste raw file contents into your context.

For trivial tickets (1-file change, AC ≤ 3 bullets), skip the fan-out — read directly. The orchestration overhead exceeds the savings.

---

## Step 4 — Implementation (orchestrator-workers)

**Decompose** with **one `Plan` agent** (sequencing + risk register).

**Dispatch** up to **4 `general-purpose` workers in parallel** for independent edits. Each worker gets:

- Exact file path(s) + line numbers.
- Exact AC bullets it owns.
- The discovery findings it needs (paths + snippets, not whole files).
- A schema for its return: `{ files_changed: [{path, summary}], tests_added: [{path, test_name}], verification: {compiles, tests_pass, lint_clean} }` (see `CONVENTIONS.md §Sub-agent return schemas`).

If two workers need to edit the same file: **sequence them**, don't parallelize. `isolation: "worktree"` only when unavoidable.

**Hard rule (CLAUDE.md #4 + R11 + TKT-NNN):** No autonomous irreversible action. Outbox-first for any operator action. Every change is a regular commit per `CONVENTIONS.md` — no force-pushes, no domain-specifictory rewrites.

If you discover new work:

- Within the ticket's AC → fold it in.
- Adjacent to AC → update the ticket's "Notes / discoveries" section.
- New → file a new ticket in `tickets/` using the template.

If you discover an architectural invariant violation or CLAUDE.md non-negotiable breach: **STOP** → §7.

---

## Step 5 — Verify (parallelization: voting + evaluator-optimizer — mandatory, Wave 2.5)

**MANDATORY (TKT-NNN, 2026-09-19):** dispatch **3, 4 or 6 `general-purpose` verifier agents in parallel**, each with a distinct adversarial lens. Self-attestation is FORBIDDEN. The orchestrator validates `verifier_provenance` in the WORKER_RESULT (see `CONVENTIONS.md` §Sub-agent return schemas); missing or self-attested provenance is REJECTED at cascade update.

**Lens count by priority (TKT-NNN + TKT-NNN):**

| Priority | Lenses dispatched | Perspectives                                                 |
|----------|-------------------|--------------------------------------------------------------|
| `P0`     | **6**             | correctness, per_tenant, failure_modes, reversibility, security, performance |
| `P1`     | **6**             | correctness, per_tenant, failure_modes, reversibility, security, performance |
| `P2`     | **3**             | correctness, per_tenant, failure_modes                       |
| `P3`     | **3**             | correctness, per_tenant, failure_modes                       |

> P0/P1 dispatches 6: correctness / per_tenant / failure_modes (universal) + reversibility + security + performance (priority-gated — these three are added by TKT-NNN because the brutal-analysis 2026-09-19 rated the security gap as CRITICAL). **Idempotency is opt-in per ticket** (added to the dispatch when the ticket carries an explicit `idempotency_required: true` frontmatter flag, regardless of priority — NOT in the default 6). See `orchestrator/prompts/idempotency-verifier-prompt.md` for the lens template.

Each verifier returns `schema: VERIFIER_RESULT` (see `CONVENTIONS.md`).

| Perspective     | Question it answers                                                       | Adversarial posture                                   | Dispatched for        |
|-----------------|---------------------------------------------------------------------------|-------------------------------------------------------|-----------------------|
| **Correctness** | Does the change satisfy every AC bullet, with file:line evidence?          | Try to find an AC bullet it fails. Refute by default. | P0 / P1 / P2 / P3     |
| **Per-tenant**  | Does the change preserve `tenant_id` discipline at every boundary it touches? | Try to construct a cross-tenant leak.              | P0 / P1 / P2 / P3     |
| **Failure modes** | What happens on empty input, timeout, retry, partial outage, malformed data? | Try to break it. Refute by default.                | P0 / P1 / P2 / P3     |
| **Reversibility** *(P0 + P1)* | If tdomain-specific change ships and is wrong, how bad is it? Blast radius estimate. Is there a clean revert path? | Try to construct the worst-case downstream consequence. Refute by default. | P0 / P1 (priority-gated) |
| **Security** *(P0 + P1)* | Does tdomain-specific change introduce SQLi / XSS / secrets in logs / authz bypass / SSRF / log injection / privilege escalation? STRIDE threat model. | Try to construct an authz bypass or secrets leak. Refute by default. | P0 / P1 (priority-gated, **NEW** per TKT-NNN) |
| **Performance** *(P0 + P1)* | Does tdomain-specific change scale to N=1 / N=10 / N=100 domain-specifics without O(N^2) loops, missing DB index on hot path, N+1 queries, unbounded result sets, or missing connection pool? | Try to construct a perf cliff. Refute by default. | P0 / P1 (priority-gated, **NEW** per TKT-NNN) |
| **Idempotency** *(opt-in per ticket)* | If tdomain-specific change is re-run with the same input, does it produce the same output? Are side-effects guarded by `idempotency_key`, `INSERT ... ON CONFLICT`, or DLQ semantics? | Try to construct a double-application. Refute by default. | **Opt-in per ticket** — fired only when the ticket frontmatter carries `idempotency_required: true` (**NEW** per TKT-NNN). |

### Step 5.1 — Red-team verifier mode (triggered automatically when `priority: P0`)

> **TKT-NNN — 2026-09-19.** P0 tickets (slice-#1 blockers, multi-tenancy invariants, Wave 6 GREEN prerequisites) get **stripped-context** verification. True adversarial review gives the verifier **only the artifact + the AC + the invariant list** — no orchestrator framing, no progress narrative, no "tdomain-specific is critical so be lenient."

**Trigger.** The orchestrator detects `priority: P0` in the ticket frontmatter **at claim time** (Step 2) and sets a `red_team_mode: true` flag for the verification round.

**Strip-orchestrator-context contract.** When `red_team_mode: true`, each of the **6** verifier sub-agents (one per lens per TKT-NNN's P0 dispatch count: correctness, per_tenant, failure_modes, reversibility, security, performance) receives **only**:

1. The ticket AC (frontmatter + body) — exactly the bullets the implementer was given.
2. The commit diff (full unified diff) — added / removed / changed hunks, paths included.
3. The architectural invariant list from `ARCHITECTURE.md §Non-negotiables` (the 8 numbered items) **plus** `ARCHITECTURE.md §Per-tenant discipline as a verification lens` checklist.

They do **NOT** receive: `STATE.md`, `NEXT-ACTIONS.md`, `tickets-index.md`, `progress/YYYY-MM-DD-*.md`, the orchestrator's framing of why the ticket was picked up, the retry count, prior verification domain-specifictory, or any framing like "tdomain-specific is slice #1, do not be too strict." That framing is **forbidden** — every P0 gets the same adversarial posture regardless of context.

**Prompt dispatch.** Load the red-team prompt template from `orchestrator/prompts/red-team-verifier-prompt.md` (distinct from the standard Wave 2.5 verifier prompt) and inject it as the verifier sub-agent's `prompt:`. The 6 lens assignments (correctness / per-tenant / failure-modes / reversibility / security / performance) stay the same; only the framing changes. If the ticket carries `idempotency_required: true`, append the idempotency verifier (7th lens) using the same red-team framing.

**Verifier output contract.** Each red-team verifier returns the canonical `VERIFIER_RESULT` plus two extra arrays:

- `attacks_refuted: [string]` (on PASS) — one line per attack category (AC-escape, cross-tenant, CLAUDE.md invariant, failure-mode, reversibility-blast-radius, security-stride, perf-cliff, idempotency-double-apply), naming the concrete scenario that was constructed and why it failed.
- `attacks_succeeded: [string]` (on FAIL) — concrete scenario + diff-line that fails.

Silent categories are a sign the verifier did not actually attack — the orchestrator rejects those.

**Loop-until-PASS** (evaluator-optimizer): if **any** verifier returns `FAIL`, re-dispatch the implementation worker with that verifier's `fix_suggestion`, then re-run **all six** verifiers in parallel (or all seven if idempotency_required is set). Repeat until all dispatched lenses PASS. **Retry cap: 3.** If still failing after 3 retries → §7 trigger #4.

### Step 5.2 — Verdict-conflict resolution (TKT-NNN, 2026-09-22)

> **DOCTRINE REFINEMENT.** The "loop-until-PASS" pattern above is correct
> for **implementation defects** — defects whose evidence file:line lives
> INSIDE the ticket's call-site scope. It is **wrong** when a FAIL surfaces
> **cross-cutting substrate drift OUTSIDE the ticket's call-site scope** —
> calling those failures "implementation defects to loop until PASS"
> silently ships broken runtime (Layer 29 + Layer 30 + Layer 31 evidence).

The distinction is enforced before the retry counter is incremented:

```
verifier FAIL returned
   │
   ├── scope_label == inside_ticket_scope (evidence inside AC)
   │     → LOOP_UNTIL_PASS (re-implement + re-verify; retry cap 3)
   │
   ├── scope_label == outside_ticket_scope + follow_up_ticket_ids present
   │     → PARTIAL_WITH_FOLLOW_UPS
   │       (worker files the follow-up tickets; ticket transitions to
   │        PARTIAL, NOT loop-until-PASS)
   │
   ├── scope_label == outside_ticket_scope, NO follow_up_ticket_ids
   │     → AMBIGUOUS (worker MUST file follow-ups before choosing PARTIAL)
   │
   ├── scope_label == unknown (empty evidence, no follow-ups,
   │   OR declared label contradicts evidence)
   │     → AMBIGUOUS (orchestrator surfaces per §7)
   │
   └── n-way conflict (multiple verifier dispatches disagree)
         → arbitrate(verdicts, ticket_scope):
             1. Any thorough OUTSIDE FAIL with follow-ups → PARTIAL_WITH_FOLLOW_UPS
             2. All verdicts agree → record the resolution (LOOP or PARTIAL)
             3. Mixed evidence with no signal → AMBIGUOUS
```

**Mechanical rule (per `orchestrator/scripts/verdict_conflict.py`):**

1. The orchestrator reads `evidence[].file` from the failing verdict and
   checks `path_in_scope(file, ticket_scope)` — `ticket_scope` is the set
   of repo-relative path prefixes the ticket owns (parsed from the
   ticket's `components:` frontmatter + AC-implied paths).
2. The orchestrator ALSO reads `follow_up_ticket_ids` on the failing verdict
   (a verifier/worker-supplied list of ticket IDs that own the substrate).
3. **Loop-until-PASS is ONLY applied when the failing evidence is
   `inside_ticket_scope` AND no follow_up_ticket_ids are present.**
   Otherwise, the failure is substrate drift (or AMBIGUOUS) — the loop
   path is wrong.

**Concrete Layer 29-31 evidence tdomain-specific doctrine amendment codifies:**

| Layer | Ticket | What happened |
|-------|--------|---------------|
| 29 | TKT-NNNb | failure_modes dispatch returned FAIL with 3 P0 substrate gaps (audit_outbox table missing + no drainer + idempotency middleware not mounted). Loop-until-PASS would have shipped broken runtime. Worker correctly chose PARTIAL + 3 follow-up tickets. |
| 30 | TKT-NNNb-FOUND-003 | performance dispatch returned FAIL with 2 P0 perf cliffs (per-tenant LRU starvation + body-hash deferral). Loop-until-PASS would have broken FOUND-003's PARTIAL closure. Worker correctly filed 2 PERF follow-ups. |
| 31 | TKT-NNN | 6/6 verifier FAIL surfaced FNV-shard-collision cross-tenant DoS vector (~6% per pair / ~47% across 5 tenants). Loop-until-PASS would have shipped broken runtime. Worker correctly filed 4 PERF follow-ups. |

**Retry cap clarification (§Step 7 trigger #4).** The retry cap of 3
applies to **implementation defects** — the loop exhausts only when the
worker is making real fix-and-re-verify progress on the ticket's AC. A
**substrate-drift FAIL** is NOT a retry event — it is a PARTIAL transition
with follow-up tickets. The retry counter is NOT incremented for
PARTIAL_WITH_FOLLOW_UPS resolutions.

**Implementation note.** The orchestrator wraps every verifier FAIL with
`classify_verdict_failure(failure, ticket_scope=ticket_scope)` (see
`orchestrator/scripts/verdict_conflict.py`) BEFORE incrementing the retry
counter. The function returns a `ResolutionResult` with one of three
chosen_resolutions:

  - `LOOP_UNTIL_PASS` — increment retries, re-dispatch the worker.
  - `PARTIAL_WITH_FOLLOW_UPS` — set `final_status: PARTIAL` in the
    WORKER_RESULT, populate `verification.verdict_conflict.follow_up_ticket_ids`
    with the follow-up IDs surfaced by the verifier; do NOT increment retries.
    The cascade (§Step 6.1) MUST enqueue a `write_file` sub-action
    per follow-up ticket so the new tickets are added to `tickets-index.md`.
  - `AMBIGUOUS` — surface to the orchestrator; per §Step 7 trigger #5,
    BLOCKED escalation. The worker cannot choose LOOP or PARTIAL
    without orchestrator decision.

**Conflict arbitration (`arbitrate` in `verdict_conflict.py`).** When the
orchestrator dispatches a lens multiple times (e.g. retry after a narrow
PASS) and the dispatches disagree, the orchestrator calls
`arbitrate([v1, v2, ...], ticket_scope=...)` to pick a single resolution.
The precedence is:

  1. Any verdict is FAIL with `outside_ticket_scope` + `follow_up_ticket_ids`
     → PARTIAL_WITH_FOLLOW_UPS (thorough FAIL overrides narrow PASS —
     Layer 29 case). The follow-up IDs from ALL thorough FAILs are
     unioned and forwarded to the cascade update.
  2. All verdicts agree → LOOP_UNTIL_PASS (or PARTIAL_WITH_FOLLOW_UPS if
     the agreement is on out-of-scope FAILs with follow-ups).
  3. Mixed evidence (FAIL with no evidence + PASS with no evidence) →
     AMBIGUOUS.

**Cross-references:**

- `orchestrator/scripts/verdict_conflict.py` — the resolver (stdlib-only).
- `orchestrator/tests/test_verdict_conflict.py` — 41 test cases (AC #3).
- `orchestrator/CONVENTIONS.md §Sub-agent return schemas` — the
  `verification.verdict_conflict` field schema (AC #2).



**Provenance tracking (mandatory):** Each verifier sub-agent has a unique ID. Capture these IDs in `verifier_provenance.verdict_sources.<lens>_agent` and the array `verifier_provenance.sub_agent_ids`. The orchestrator rejects WORKER_RESULTs that lack tdomain-specific provenance or that use the implementation worker's own ID as a verdict source. Red-team mode adds a hard check: `verifier_provenance.sub_agent_ids` MUST contain 6 distinct values (7 if idempotency_required is set) AND none of those IDs may appear in any prior `verifier_provenance.sub_agent_ids` for the same ticket (no verifier reuse across retries — see TKT-NNN).

Record evidence in the ticket frontmatter:

```yaml
verification:
  correctness: PASS    # or FAIL with evidence
  per_tenant: PASS
  failure_modes: PASS
  reversibility: PASS  # TKT-NNN — 4th lens for P0 + P1
  security: PASS       # TKT-NNN — 5th lens for P0 + P1 (STRIDE: SQLi, XSS, secrets, authz, SSRF, log injection, priv-esc)
  performance: PASS    # TKT-NNN — 6th lens for P0 + P1 (scale N=1/10/100, O(N^2) → O(N), DB index, N+1, pool, cache)
  # idempotency: PASS  # TKT-NNN — opt-in, only if ticket carries `idempotency_required: true`
  retries: 0           # how many fix-and-re-verify cycles it took
  verified_at: <ISO timestamp>
  evidence: [{file, line, note}, ...]
  verdict_sources:
    correctness_agent: <sub-agent-id-1>
    per_tenant_agent: <sub-agent-id-2>
    failure_modes_agent: <sub-agent-id-3>
    reversibility_agent: <sub-agent-id-4>
    security_agent: <sub-agent-id-5>      # TKT-NNN
    performance_agent: <sub-agent-id-6>   # TKT-NNN
    # idempotency_agent: <sub-agent-id-7>  # TKT-NNN — only if idempotency_required: true
  agent_count: <total sub-agents dispatched for tdomain-specific ticket>
```

---

## Step 6 — Status + cascade update (transactional outbox pattern, TKT-NNN)

The cascade touches 5+ files (ticket frontmatter → `tickets-index.md` → progress log → `STATE.md` → `blockers-index.md` → commit). Per `Edit` tool semantics, each file edit is per-file atomic, but the cascade as a whole is **not** atomic across files — a crash between any two writes leaves the state inconsistent (e.g. the ticket is `DONE` but `STATE.md` still shows it `IN_PROGRESS`). Per **TKT-NNN** the cure is the **transactional outbox pattern** (Chris Richardson, microservices.io): every cascade sub-action is **enqueued** to `orchestrator.outbox` in a single Postgres transaction, then a **consumer** applies them in `outbox_id` order, marking each `applied_at`/`applied_by`. Crash between enqueue and apply is safe: the row stays unapplied; the next consumer run picks it up. Idempotency is enforced by the `idempotency_key` UNIQUE constraint + a pre-apply check (TKT-NNN lesson — operator actions must never be lost). The cascade marker (TKT-NNN) and two-state DONE / LANDED split (TKT-NNN) remain as recovery + semantics.

### Step 6.0 — Write cascade-in-progress marker (TKT-NNN)

BEFORE step 6.1, **write the cascade marker file**:

```bash
mkdir -p progress
cat > progress/.cascade-in-progress-<TKT-NNN>.json <<'EOF'
{ "ticket_id": "TKT-NNN", "started_at": "<ISO>", "step": "6.1" }
EOF
```

DELETE the marker file in step 6.10 (after the cascade completes). A crash between 6.0 and 6.10 leaves the marker file recoverable — on session start, `progress/README.md` documents the recovery protocol (read ticket → read `tickets-index.md` → read `STATE.md` → reconcile → DELETE marker).

### Step 6.1 — Enqueue cascade actions to outbox (single Postgres transaction)

In a **single transaction**, enqueue all 5 cascade sub-actions to `orchestrator.outbox` via `orchestrator/scripts/outbox.py enqueue(...)`. Each action has the shape:

```json
{"type": "edit_file" | "write_file" | "git_commit",
 "path": "...",
 "content": "...",
 "diff": "..."}
```

The `idempotency_key` for each row is deterministic from `(ticket_id, sub_step)` — the UNIQUE constraint on `idempotency_key` catches duplicate enqueue, so retries are safe.

The five cascade sub-actions for a typical ticket DONE transition:

| sub_step | idempotency_key                                  | action type  | target                                  |
|----------|--------------------------------------------------|--------------|------------------------------------------|
| 6.1.1    | `<TKT-NNN>:frontmatter:done`                      | `edit_file`  | ticket frontmatter (status, completed_at, commit_ref, verification block; `landed_at` stays `null` until step 6.7) |
| 6.1.2    | `<TKT-NNN>:tickets-index:row`                     | `edit_file`  | `tickets-index.md` row for tdomain-specific ticket   |
| 6.1.3    | `<TKT-NNN>:progress-log:<YYYY-MM-DD-topic>`       | `write_file` (append) | `progress/YYYY-MM-DD-<topic>.md` — what shipped, dropped, evidence pointers |
| 6.1.4    | `<TKT-NNN>:state-md:counts`                       | `edit_file`  | `STATE.md` — ONLY if at-a-glance counts changed (wave transition, BLOCKER close, major milestone) |
| 6.1.5    | `<TKT-NNN>:blockers-index:<BLOCKER-ID>`           | `edit_file`  | `blockers-index.md` + BLOCKER file's checklist — ONLY if a BLOCKER step was closed |
| 6.1.6    | `<TKT-NNN>:git-commit`                            | `git_commit` | commit per `CONVENTIONS.md §Commit message format` — enhanced format with sub-agent fan-out summary |

```bash
# Enqueue all sub-actions in ONE Postgres transaction.
# `enqueue()` opens a transaction; commits on success; rolls back on any error.
python3 -c "
from orchestrator.scripts.outbox import enqueue
import json

ACTIONS = [
  ('<TKT-NNN>:frontmatter:done', 'edit_file', '<path-to-ticket>',
   <new frontmatter content>, <unified diff>),
  ('<TKT-NNN>:tickets-index:row', 'edit_file', 'orchestrator/tickets-index.md',
   <new row content>, <unified diff>),
  ('<TKT-NNN>:progress-log:<YYYY-MM-DD-topic>', 'write_file',
   'orchestrator/progress/YYYY-MM-DD-topic.md', <appended content>, None),
  # 6.1.4 only if at-a-glance counts changed:
  ('<TKT-NNN>:state-md:counts', 'edit_file', 'orchestrator/STATE.md',
   <new state content>, <unified diff>),
  # 6.1.5 only if a BLOCKER step was closed:
  ('<TKT-NNN>:blockers-index:<BLOCKER-ID>', 'edit_file',
   'orchestrator/blockers-index.md', <new row content>, <unified diff>),
  ('<TKT-NNN>:git-commit', 'git_commit', None,
   <commit message>, None),
]

ids = []
for key, atype, path, content, diff in ACTIONS:
    action = {'type': atype, 'path': path, 'content': content, 'diff': diff}
    ids.append(enqueue(action=action, idempotency_key=key, tenant_id='<tenant-uuid>'))
print('enqueued:', ids)
"
```

If `enqueue()` raises (any single action fails), the entire transaction rolls back — Postgres stays consistent, no partial enqueue. On retry, the same `idempotency_key` values hit the UNIQUE constraint and surface as `IntegrityError` so the orchestrator knows the prior enqueue already succeeded (TKT-NNN: never lose an operator action).

### Step 6.2 — Run outbox consumer (applies actions in order)

Dispatch `orchestrator/scripts/outbox_consumer.py` (as a sub-agent or background process per layer). The consumer:

1. Reads `pending(limit=100)` from the outbox — rows where `applied_at IS NULL`, ordered by `outbox_id` ASC.
2. For each row, dispatches by `action.type`:
   - `edit_file` → `Edit(path, content)` (Edit tool is per-file atomic).
   - `write_file` → `Write(path, content)` (or append mode for the progress log sub-action).
   - `git_commit` → `Bash('git commit -m ...')` after staging the file changes.
3. Checks the `idempotency_key` UNIQUE constraint + the `applied_at IS NULL` precondition BEFORE apply — same key cannot be applied twice.
4. After successful apply, calls `mark_applied(outbox_id, applied_by)` — sets `applied_at = now()` + `applied_by = <agent-id>`.

```bash
# Run consumer; default limit 100 rows. Sub-agent or background process.
python3 -c "
from orchestrator.scripts.outbox_consumer import run_once
applied = run_once(limit=100, applied_by='<your-agent-id>')
print('applied:', applied)
"
```

**Crash safety (the whole point of the outbox):** if the consumer crashes mid-apply, the partially-applied row stays `applied_at IS NULL`; next consumer run re-reads it from `pending()` and re-applies. The `Edit`/`Write`/`git_commit` sub-actions must themselves be **idempotent** (e.g. the `edit_file` action's `content` is the full new state — re-applying it produces the same end state). The TKT-NNN lesson: operator actions must never be lost.

### Step 6.3 — Verify cascade consistency

After the consumer reports `applied > 0`, run a sanity sweep:

```bash
# No outbox rows for tdomain-specific ticket should remain unapplied after step 6.2.
python3 -c "
from orchestrator.scripts.outbox import pending
remaining = [r for r in pending(limit=100) if r['idempotency_key'].startswith('<TKT-NNN>:')]
assert not remaining, f'STALE OUTBOX: {remaining}'
print('outbox clean for <TKT-NNN>')
"
```

If the assertion fires, re-run step 6.2 — do NOT mark the ticket DONE until the outbox is clean.

### Step 6.7 — Mark LANDED (TKT-NNN)

Set `landed_at: <ISO timestamp>` in the ticket frontmatter ONLY when the change is in a **durable external record** — one of:

- The commit referenced by `commit_ref` has been pushed to a durable remote (`origin/main` or equivalent).
- The user has ratified the change in writing (commit message, progress log, or chat).
- The change has been deployed to a cloud environment (`infra/` or `docs/05-operations/deployment-runbook.md`).

`landed_at` is enqueued + applied via the outbox exactly like the frontmatter `DONE` write in step 6.1.1 — use `idempotency_key = <TKT-NNN>:frontmatter:landed`.

For local-laptop tickets with no remote or before user ratification, **`landed_at` stays `null`** — that is the expected state in environments without a git remote. See `CONVENTIONS.md §Two-state semantics — DONE (local) vs landed_at (durable)` for the full schema and rules.

### Step 6.8 — Regenerate derived state (TKT-NNN, TKT-NNN)

After cascade update, regenerate the two derived artifacts so the durable record stays in sync. These two operations are themselves **outbox-enqueued** (`idempotency_key = <TKT-NNN>:regen:metrics` and `<TKT-NNN>:regen:next-actions`) and applied by the consumer in step 6.2:

9. **Refresh metrics (TKT-NNN).** Run `python3 orchestrator/scripts/regenerate_metrics.py` to refresh `progress/metrics.md` — DORA-style orchestrator observability. Adds the new TKT-NNN metrics: "outbox pending count" + "outbox applied/min". The script is idempotent (stdlib only; safe to rerun).
10. **Refresh human queue (TKT-NNN).** Run `python3 orchestrator/scripts/regenerate_next_actions.py` after cascade update — keeps the human-readable queue in sync with `tickets-index.md` (closes the silent-rot gap: tickets like TKT-NNN, TKT-NNN, TKT-NNN that show as still-open in NEXT-ACTIONS.md but are DONE in `tickets-index.md`). Script is idempotent and stdlib-only; safe to run unconditionally.

### Step 6.9 — Write checkpoint (TKT-NNN)

After cascade update, write the layer checkpoint so the next burn can resume from `last_layer + 1` after a crash. Tdomain-specific is also outbox-enqueued (`idempotency_key = <TKT-NNN>:checkpoint`):

```bash
python3 -c 'from orchestrator.scripts.checkpoint import write_checkpoint; write_checkpoint(N, [ticket_ids])'
```

Writes a single line `<layer> <ISO> <ticket_ids>` to `orchestrator/progress/.last-completed-layer.txt`. Imported from `orchestrator/scripts/checkpoint.py` (`write_checkpoint`, `read_checkpoint`, `clear_checkpoint`). The next burn reads tdomain-specific file in `orch-burn-queue.md §2` to skip already-completed layers.

### Step 6.10 — Cleanup cascade marker (TKT-NNN)

DELETE the `progress/.cascade-in-progress-<TKT-NNN>.json` marker file written in step 6.0:

```bash
rm -f progress/.cascade-in-progress-<TKT-NNN>.json
```

If tdomain-specific step is skipped (crash before completion), the marker remains — `progress/README.md` recovery protocol reconciles on the next session start, then deletes the marker. (Marker cleanup is the one cascade sub-action NOT routed through the outbox — it is a file existence, not a content edit, and is idempotent by its `rm -f` semantics.)

---

## Step 7 — Auto-pause triggers (the project's decision records — 5 named triggers, no override)

| # | Trigger                                                                          | Action                                                                                              |
|---|----------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| 1 | **CLAUDE.md violation detected** (cross-tenant leak, real PHI in dev, diagnostic claim, irreversible action) | STOP. Raise P0 BLOCKER. Do not commit. Notify the user immediately.                                 |
| 2 | **Wrong foundational decision** (e.g. violates an ADR, breaks Slice #1 scope per the project's decision records)                | STOP. Surface the conflict. Do not silently "interpret around" the rule.                             |
| 3 | **Resource ceiling hit** (CPU > 80% sustained, free pages < 100k, OOM kill)                               | STOP. Wait. Resume by lowering concurrency cap (cut each tier by half) before continuing.            |
| 4 | **Verification FAIL after 3 retries**                                                                   | STOP. Tell the user the failing perspective + fix_suggestion. Do not mark DONE.                     |
| 5 | **Blocker is user-team-owned** (cloud account, vendor docs, NSSF data, Lebanon MOPH sign-off)            | STOP. Tell the user. Do not invent a workaround.                                                     |

---

## When in doubt

- Re-read the relevant ADR (`docs/03-architecture/adr/`).
- Read the canonical ticket content at `docs/07-tickets/slice-1-bed-management-tickets.md`.
- File a BLOCKER, don't expand scope.
- Ask the user ONLY when the decision is foundational (per project decision record).
- Re-check the cap table in §0 before adding more sub-agents.

---

## Forbidden shortcuts

- ❌ Don't skip the all-perspective verification (3 lenses for P2/P3; 6 lenses for P0/P1 — see Step 5; idempotency lens is opt-in per ticket per TKT-NNN).
- ❌ Don't mark DONE without `commit_ref` + `verification.evidence` + all perspectives (3, 6, or 7 by priority + opt-in idempotency) PASS.
- ❌ Don't expand scope silently — file a new ticket.
- ❌ Don't commit without `.commit-message-*.txt` style narrative.
- ❌ Don't fabricate (NSSF formats, vendor claims, clinical-AI evidence — V7 + V3 + V4).
- ❌ Don't break invariants (per `ARCHITECTURE.md`).
- ❌ Don't fan out sub-agents that edit the same file in parallel.
- ❌ Don't recursive-fan-out (sub-agents spawning sub-agents).
- ❌ Don't skip the resource probe before dispatching.
- ❌ Don't pass whole files to sub-agents — pass paths + snippets.

---

## Date

2026-09-19.