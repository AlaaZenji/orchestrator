# Architectural Invariants

> **Every AI agent must respect these.** Violating any of them is a P0 BLOCKER per CLAUDE.md.
> Tdomain-specific file is a pointer + summary. The canonical truth is in `<project_root>/CLAUDE.md` and `<project_root>/docs/03-architecture/adr/`.

## Non-negotiables (per CLAUDE.md)

1. **Modularity.** Every domain-specific must be able to adopt one module or many.
2. **Multi-tenancy.** Every component supports multi-tenant isolation from day one. Cross-tenant boundary crossings without RLS / per-tenant key isolation = STOP + raise P0 BLOCKER.
3. **No real domain-specific data in development.** Synthetic data only. Real PHI in dev = STOP + raise P0 BLOCKER.
4. **AI safety.** No autonomous irreversible action. Every automated recommendation requires human approval (per R11 lessons).
5. **Operations intelligence, not medical device.** (the project's decision records / V5). No diagnostic / treatment claims in marketing or product copy.
6. **EU AI Act high-risk-ready-by-design.** (the project's decision records). Every AI feature ships with documented external validation + subgroup metrics + drift monitoring.
7. **Offline-first.** (the project's decision records). Local-first persistence + outbox + sync-when-connected. Multi-region does NOT substitute.
8. **External validation discipline.** (R11 + V2). Every model ships with documented external-validation report + subgroup metrics + drift monitoring.

## Per-language standards (per project decision record)

- **Java 21** — the project's primary backend + the project's primary HL7v2 + Bundle translator + Camel HL7.
- **Python 3.12** — offline-sync engine + synthetic-data generator.
- **TypeScript strict** — UI + WhatsApp integration.
- **Go** — audit-writer (hash-chained immutable log).
- **SQL** with `sqlfluff` + Flyway + Postgres RLS on every clinical/business table.

## Per-tenant discipline (every signature)

- `tenant_id` as the first column of every primary key.
- Per-tenant CMK envelope encryption via cloud-provider KMS (UAE primary per the project's decision records).
- Per-tenant FHIR endpoint, per-tenant cache namespacing, per-tenant audit-log schema.
- No global mutable state. No cross-tenant shortcuts. No shared caches.

## Per-tenant discipline as a verification lens (Wave 2.5 normative)

The **per-tenant verifier lens** (one of the 3 Wave 2.5 perspectives, see `WORKFLOW.md §5`) is not optional. Every ticket landing must demonstrate, with file:line evidence:

| Check                                                         | Required evidence                                  |
|---------------------------------------------------------------|----------------------------------------------------|
| `tenant_id` is the first PK column on every new clinical/business table | Migration file + line                              |
| RLS is enabled AND forced on every new clinical/business table | Migration file + `\d+ <table>` psql output path    |
| A tenant-isolation policy exists AND uses `current_setting('app.tenant_id')` | Policy definition + line                            |
| No cross-tenant JOIN, no cross-tenant cache key, no shared mutable state | Code reviewer (the lens agent) constructs a counter-example and either refutes or fails the ticket |
| Per-tenant CMK envelope encryption references the per-tenant key, not a global key | Config file + line                                 |

If any check lacks file:line evidence, the per-tenant lens **fails by default** (adversarial posture).

## Autonomy posture (the project's decision records)

- **4 named gates:** end of Wave 2.5, end of Wave 3, end of Wave 4, end of slice #1.
- **5 auto-pause conditions** (see `WORKFLOW.md §7` for the operational handling):
  1. Token budget at 95% stop.
  2. CLAUDE.md violation.
  3. Wrong foundational decision.
  4. **Resource ceiling hit** (CPU > 80% sustained, free pages < 100k, OOM kill) — protects the laptop under heavy sub-agent fan-out.
  5. **Verification FAIL after 3 retries** — protects against the loop-until-PASS harness running away.
- Per the project's decision records, micro-decisions do NOT require explicit user approval — proceed unless one of the auto-pauses triggers.

## Verification discipline (Wave 2.5 — voting + evaluator-optimizer)

Every Phase ticket landing requires **3-perspective adversarial verification** (parallel dispatch, see `WORKFLOW.md §5`):

1. **Correctness** — vs ticket AC + canonical doc. File:line evidence required.
2. **Per-tenant + ADR alignment** — see the normative checklist above.
3. **Failure modes** — what breaks first under stress? What's the recovery? Tests for failure modes?

**Loop-until-PASS** (evaluator-optimizer): if any perspective fails, re-dispatch fix + re-verify, **retry cap = 3**. Record `retries: N` in the ticket frontmatter. Exceeding the cap → auto-pause trigger #4.

## Industry-standard multi-agent patterns (canonical reference)

The orchestrator implements the canonical Anthropic multi-agent patterns. Each is wired into `WORKFLOW.md`:

| Pattern                    | Operational hook                                                                 |
|----------------------------|----------------------------------------------------------------------------------|
| **Orchestrator-workers**   | `WORKFLOW.md §4` — one `Plan` agent decomposes, up to 4 `general-purpose` workers in parallel for independent edits. |
| **Parallelization: sectioning** | `WORKFLOW.md §3` — discovery fan-out by 6 named concerns (code / spec / risk / tenant / test / prior-art). |
| **Parallelization: voting** | `WORKFLOW.md §5` — 3 verifier agents with distinct adversarial lenses, parallel dispatch. |
| **Evaluator-optimizer**    | `WORKFLOW.md §5` — loop-until-PASS with retry cap 3.                             |
| **Prompt chaining**        | Steps 0→7 are a gated chain — each gate's output is the next gate's input.       |
| **Routing**                | `WORKFLOW.md §2` — explicit ticket argument vs `NEXT-ACTIONS.md` top-QUEUED.    |

**Hard rules** (operational, see `WORKFLOW.md §0`):

- Two sub-agents editing the same file must NOT run in parallel.
- No recursive fan-out (depth cap = 2).
- Pass `schema:` (JSON Schema) at every sub-agent dispatch site — see `CONVENTIONS.md §Sub-agent return schemas`.
- Cap concurrency from the pre-flight probe — see the cap table in `WORKFLOW.md §0`.

## Outbox-first lesson (TKT-NNN — 2026-09-15)

> **Operator actions: outbox first, audit emission best-effort. Losing an operator action is a CLAUDE.md #4 violation.**

If you ever feel tempted to write the audit event before the outbox row, stop and re-read the ticket.

## Slice #1 boundary (the project's decision records)

- Slice #1 = **Bed Management + Command Center ONLY.**
- Slice #2 (RCM) and Slice #3 (ED Throughput) are DEFERRED.
- Do not expand slice #1 scope without filing an ADR deviation.

## HL7 v2 lessons (the project's decision records + V8)

- MLLP-over-TLS ONLY — plaintext listener forbidden.
- IP allow-list before message parse.
- ACK mode `AL` (always) is default; `NE` rejected at config time.
- A08 / PID / MRG mutations detected pre-translation trigger audit.
- the project's primary HL7v2 + Camel primary, NOT Mirth Connect OSS.

## ADR pointer (the 22 decisions)

| # | Decision | File |
|---|----------|------|
| the project's decision records | the project's primary backend (Apache-2.0) on Postgres | `docs/03-architecture/adr/0001-hapi-fhir-as-v1-fhir-server.md` |
| the project's decision records | Terminology subset: ICD-11 + LOINC + ATC + UCUM + small SNOMED anatomy | `docs/03-architecture/adr/0002-terminology-subset-for-v1.md` |
| the project's decision records | Operations intelligence, not medical device | `docs/03-architecture/adr/0003-positioning-operations-intelligence-not-medical-device.md` |
| the project's decision records | InterSystems TrakCare primary Tier-1 domain-specific partner | `docs/03-architecture/adr/0004-intersystems-trakcare-as-primary-tier-1-domain-specific-partner.md` |
| the project's decision records | Sub-100-bed OSS foundation | `docs/03-architecture/adr/0005-sub-100-bed-oss-foundation.md` |
| the project's decision records | Autonomy posture (4 gates + 5 auto-pauses) | `docs/03-architecture/adr/0006-autonomy-posture-4-gates-and-auto-pauses.md` |
| the project's decision records | Pilot domain-specific selection criteria | `docs/03-architecture/adr/0007-pilot-domain-specific-selection-criteria.md` |
| the project's decision records | Multi-tenancy = SaaS with strong isolation | `docs/03-architecture/adr/0008-multi-tenancy-mandated-strong-isolation.md` |
| the project's decision records | Slice #1 = Bed Management + Command Center only | `docs/03-architecture/adr/0009-first-vertical-slice-scoping-refinement.md` |
| the project's decision records | AI inference posture = hybrid | `docs/03-architecture/adr/0010-ai-inference-posture-hybrid.md` |
| the project's decision records | Lebanon RCM is local, not Waystar clone | `docs/03-architecture/adr/0011-lebanon-rcm-is-local-not-waystar-clone.md` |
| the project's decision records | Per-bed tiered pricing + free Community tier | `docs/03-architecture/adr/0012-pricing-posture-per-bed-tiered.md` |
| the project's decision records | Geographic ambition: Lebanon → MENA → SSA + S Asia | `docs/03-architecture/adr/0013-geographic-ambition-lebanon-then-mena-then-ssa.md` |
| the project's decision records | Offline-first architecture | `docs/03-architecture/adr/0014-offline-first-architecture.md` |
| the project's decision records | Three-compliance-gate framework | `docs/03-architecture/adr/0015-three-compliance-gates-framework.md` |
| the project's decision records | Compliance evidence engineering discipline | `docs/03-architecture/adr/0016-compliance-evidence-engineering-discipline.md` |
| the project's decision records | HL7 v2.5.1 first-class integration (V8-corrected) | `docs/03-architecture/adr/0017-hl7-v2-first-class-integration.md` |
| the project's decision records | DICOMweb imaging fabric | `docs/03-architecture/adr/0018-dicomweb-imaging-fabric.md` |
| the project's decision records | WhatsApp as clinical channel | `docs/03-architecture/adr/0019-whatsapp-as-clinical-channel.md` |
| the project's decision records | Open-source admin backbone | `docs/03-architecture/adr/0020-open-source-admin-backbone.md` |
| the project's decision records | Defer touchless PA + autonomous coding | `docs/03-architecture/adr/0021-defer-ai-first-rcm-and-touchless.md` |
| the project's decision records | EU AI Act high-risk-ready-by-design | `docs/03-architecture/adr/0022-eu-ai-act-high-risk-ready-by-design.md` |

## Sources

- `<project_root>/CLAUDE.md`
- `<project_root>/docs/03-architecture/adr/`
- `<project_root>/docs/04-engineering/coding-standards.md`
- `<project_root>/memory/tkt-p1-011-outbox-first-lesson.md`
- `<project_root>/memory/wave-2-5-adversarial-cadence.md`
- `<project_root>/memory/adr-0006-autonomy-posture.md`

## Date

2026-09-19.