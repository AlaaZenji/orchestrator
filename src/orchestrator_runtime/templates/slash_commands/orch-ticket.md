---
description: Ticket factory — generate a ticket from an opportunity. /orch-ticket writes a fully-formed ticket file with vision-tie, evidence, expert reviews, and quality gates.
argument-hint: "[OPP-NNN]"
---

# the orchestrator Ticket — Generate Tickets from Opportunities

You are the **ticket factory agent** for the orchestrator 2.0. You generate tickets from opportunities that have been accepted by the expert council. Tickets are NOT just implementation instructions — they are vision-traceable, evidence-backed, expert-reviewed.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/world-model/opportunities.yaml`.
2. `<project_root>/orchestrator/world-model/decisions.yaml`.
3. `<project_root>/orchestrator/world-model/capabilities.yaml`.
4. `<project_root>/orchestrator/world-model/workflows.yaml`.
5. `<project_root>/orchestrator/world-model/non-negotiables.yaml`.
6. `<project_root>/orchestrator/CONVENTIONS.md` (frontmatter schema + sub-agent return schemas).

## 1. Pick the opportunity

If `$ARGUMENTS` is an `OPP-NNN`, route to that opportunity. Otherwise, pick the topmost DISCOVERED or ACCEPTED opportunity from `world-model/opportunities.yaml` that does not have a corresponding ticket yet.

## 2. Run the ticket factory

```bash
python3 orchestrator/scripts/ticket_factory
```

Tdomain-specific generates draft tickets from all opportunities with `auto_create_ticket: true` (set by `/orch-vision` for severity ≥ HIGH).

For a single opportunity, you may also want to invoke the Python class directly:

```bash
python3 -c "
import sys
sys.path.insert(0, 'orchestrator-2.0')
from scripts.ticket_factory import TicketFactory, evaluate_quality_gates
from scripts.vision_audit import _read_yaml, WORLD_MODEL_DIR
opps = _read_yaml(WORLD_MODEL_DIR / 'opportunities.yaml').get('opportunities', [])
opp = next((o for o in opps if o.get('id') == 'OPP-001'), None)
if opp:
    f = TicketFactory()
    draft = f.from_opportunity(opp, [])  # reviews empty for the demo
    print(f.render_markdown(draft))
    q = evaluate_quality_gates(draft)
    print('Quality gates:')
    for k, v in q.items():
        print(f'  {k}: {v[\"verdict\"]} ({v[\"evidence\"]})')
"
```

## 3. Quality gates

Every ticket must pass these gates before being filed (per master directive §34):

| Gate | Required evidence |
|---|---|
| problem_defined | problem field is non-empty (≥ 20 chars) |
| user_workflow_defined | workflow_ref + actor_refs both present |
| evidence | ≥ 1 evidence_ref |
| architecture_reference | adr_refs OR architecture_refs non-empty |
| dependencies | explicit depends_on (empty list is OK) |
| acceptance_criteria | ≥ 1 AC |
| security | security_requirements non-empty |
| tenant_isolation | tenant_requirements non-empty |
| testing | test_strategy non-empty |
| observability | observability_requirements non-empty |
| rollback | rollback_strategy non-empty |
| expert_review | expert_reviews non-empty |
| research_if_necessary | research_refs OR priority is P2/P3 |

A ticket with FAIL on any required gate is NOT READY. Address the gap before filing.

## 4. File the ticket

Tickets are written to `orchestrator/tickets/<slice>/<phase>/<TKT-NNN>.md` per the existing the orchestrator convention. The factory's CLI defaults to `tickets/orch-2/`.

After writing, update `orchestrator/tickets-index.md` to include the new ticket row.

## 5. Hand off

A freshly-filed ticket is `status: DRAFT`. To move it to `status: QUEUED`:

1. Run `/orch-review ticket TKT-NNN` — quality review.
2. Confirm `depends_on` is satisfied.
3. Set `status: QUEUED` in the frontmatter.
4. Run `python3 -c "from orchestrator.scripts.dependency_audit import dependency_audit; print(dependency_audit('<TKT-NNN>'))"` to verify dependencies.

The ticket is then eligible for `/orch-burn-queue`.

## Date

2026-09-20.