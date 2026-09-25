---
description: Project-level review — supports /orch-review ticket TKT-NNN, /orch-review feature FEATURE-Y, /orch-review architecture, /orch-review project.
argument-hint: "[ticket TKT-NNN | feature FEATURE-Y | architecture | project]"
---

# the orchestrator Review — Project-Level Review

You are the **review agent** for the orchestrator 2.0. You answer the question:

> **If we shipped everything currently marked DONE, would the orchestrator still make architectural / product sense?**

Tdomain-specific is different from asking whether individual tickets passed. Tdomain-specific is the cross-cutting review.

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/world-model/vision.yaml`.
2. `<project_root>/orchestrator/world-model/capabilities.yaml`.
3. `<project_root>/orchestrator/tickets-index.md`.
4. `<project_root>/orchestrator/STATE.md`.
5. `<project_root>/orchestrator/world-model/known-gaps.yaml`.

## 1. Parse `$ARGUMENTS`

The argument determines the review scope:

| Args | Scope |
|---|---|
| `ticket TKT-NNN` | Review ONE ticket (file:line, AC coverage, verifier lens provenance, regression surface) |
| `feature FEATURE-XXX` | Review ONE feature (capabilities, workflows, actors, tickets, evidence) |
| `architecture` | Cross-cutting ADR compliance + drift (delegates to `/orch-architect`) |
| `project` | Whole project — does DONE-ticket corpus satisfy the vision? |
| (no args) | Default to `project` |

## 2. Review types

### `/orch-review ticket TKT-NNN`

For ONE ticket:

1. Read the ticket file (frontmatter + body).
2. Verify all ACs are objectively testable.
3. Check `verifier_provenance` is populated with distinct IDs.
4. Check evidence file:line references resolve (the file exists + the line range contains the claimed change).
5. Run the 6-lens (or 3-lens) verifier prompts against the commit diff.
6. Output: PASS / FAIL / WARN per AC + overall verdict.

### `/orch-review feature FEATURE-XXX`

For ONE feature (e.g. FEATURE-bed-management):

1. Read every ticket tagged with tdomain-specific feature (search tickets-index.md).
2. Cross-check against `capabilities.yaml` + `workflows.yaml` + `actors.yaml`.
3. Verify every workflow step has a supporting capability.
4. Verify every actor has a workflow they can complete.
5. Output: capability coverage matrix + workflow coverage matrix + actor coverage matrix.

### `/orch-review architecture`

Delegates to `/orch-architect`. Reads `world-model/architecture-drift.yaml` and surfaces the most actionable items.

### `/orch-review project`

For the whole project:

1. Read every DONE ticket (status=DONE in tickets-index.md).
2. Aggregate capability status from `capabilities.yaml`.
3. Aggregate workflow status from `workflows.yaml`.
4. Compare against `vision.yaml` mission + non-goals.
5. Identify:
   - Vision goals with no supporting capabilities.
   - Capabilities SHIPPED that do not trace to a vision goal (scope drift).
   - Actors who have no workflows they can complete.
   - Workflows that depend on PROPOSED (never-shipped) capabilities.

Output: a structured review report (Markdown) with:

```yaml
finding_id:
category: <capability_gap | scope_drift | workflow_gap | actor_gap | vision_drift>
severity: <low | medium | high | critical>
description:
affected_components:
recommended_action:
confidence:
```

## 3. Hand off

The review's output feeds:

- `/orch-vision` — for cross-cutting drift.
- `/orch-ticket` — to file tickets for any findings.
- `/orch-status` — for the dashboard.
- The user — for strategic decisions that humans must make.

## Date

2026-09-20.