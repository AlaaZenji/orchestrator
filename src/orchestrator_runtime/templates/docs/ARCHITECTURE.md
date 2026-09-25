# Architectural Invariants

> The non-negotiable rules every project bootstrapped via `/setup-project`
> must respect. The orchestrator's verification discipline enforces these
> on every ticket landing.

## Non-negotiables

1. **Modularity.** Every project must be decomposable into independent modules.
2. **Multi-tenancy (optional).** When enabled, every shared table MUST have RLS.
3. **Synthetic data in development.** No real PII / PHI / sensitive data in dev/test.
4. **AI safety.** No autonomous irreversible action. Every recommendation requires human approval.
5. **No diagnostic / treatment claims in marketing copy** (the project must define its own boundary).
6. **EU AI Act high-risk-ready-by-design** (where applicable). External validation + subgroup metrics + drift monitoring.
7. **Offline-first.** Local-first persistence + outbox + sync-when-connected.
8. **External validation discipline.** Every model ships with documented external-validation report.

> Adapt this list to the project's domain. The structure is portable; the content is yours.

## Per-language standards (template)

- **Java / Kotlin / Scala** — integration layer (when JVM is the project's primary).
- **Python 3.12** — offline-sync engine + synthetic-data generator (when Python is the project's primary).
- **TypeScript strict** — UI (when TS is the project's primary).
- **Go** — immutable hash-chained audit log (when Go is the project's primary).
- **SQL** with the project's chosen linter + migration tool + Postgres RLS on every business table.

## Per-tenant discipline (only if multi-tenant)

- `tenant_id` as the first column of every primary key.
- Per-tenant CMK envelope encryption via cloud-provider KMS.
- Per-tenant cache namespacing, per-tenant audit-log schema.
- No global mutable state. No cross-tenant shortcuts. No shared caches.

## Verification discipline

Every Phase ticket landing requires 3-perspective adversarial verification (parallel dispatch):

1. **Correctness** — vs ticket AC + canonical doc. File:line evidence required.
2. **Per-tenant + ADR alignment** — see the per-tenant checklist (skip if not multi-tenant).
3. **Failure modes** — what breaks first under stress? What's the recovery? Tests for failure modes?

For higher-stakes tickets (P0/P1), also dispatch: reversibility, security, performance. For idempotency-sensitive changes, add the idempotency lens.

**Loop-until-PASS**: if any perspective fails, re-dispatch fix + re-verify, retry cap = 3. Record `retries: N` in the ticket frontmatter.

## Autonomy posture

The orchestrator ships with 4 named gates (per ADR-style autonomy posture). The exact gates are project-specific; the structure is:

- 4 named pause points where the orchestrator auto-pauses for human review
- 5 auto-pause conditions: budget exceeded, invariant violation, wrong foundational decision, resource ceiling hit, verification FAIL after 3 retries

Per the orchestrator's design, micro-decisions do NOT require explicit user approval — proceed unless an auto-pause triggers.

## Multi-agent patterns (canonical reference)

The orchestrator implements the canonical patterns:

| Pattern | Operational hook |
|---|---|
| **Orchestrator-workers** | One Plan agent decomposes, up to 4 general-purpose workers in parallel for independent edits |
| **Parallelization: sectioning** | Discovery fan-out by 6 named concerns (code / spec / risk / tenant / test / prior-art) |
| **Parallelization: voting** | 3-6 verifier agents with distinct adversarial lenses, parallel dispatch |
| **Evaluator-optimizer** | Loop-until-PASS with retry cap 3 |
| **Prompt chaining** | Steps 0→N are a gated chain — each gate's output is the next gate's input |
| **Routing** | Explicit ticket argument vs top-QUEUED |

**Hard rules:**

- Two sub-agents editing the same file must NOT run in parallel.
- No recursive fan-out (depth cap = 2).
- Pass `schema:` (JSON Schema) at every sub-agent dispatch site.
- Cap concurrency from the pre-flight resource probe.

## Outbox-first lesson

> Operator actions: outbox first, audit emission best-effort.
> Losing an operator action is a CLAUDE.md non-negotiable violation.

If you ever feel tempted to write the audit event before the outbox row, stop and re-read this rule.

## ADR pointer

The project's decision records (ADRs) live at `docs/architecture/`. Each one is a numbered decision with Context / Alternatives / Decision / Consequences. The orchestrator does not enforce any specific ADR — the project's team does.

## Sources

- The orchestrator's WORKFLOW.md (Steps 0-N of the ticket lifecycle).
- The orchestrator's CONVENTIONS.md (status enum, ticket schema, lease protocol).
- The project's own CLAUDE.md non-negotiables.
