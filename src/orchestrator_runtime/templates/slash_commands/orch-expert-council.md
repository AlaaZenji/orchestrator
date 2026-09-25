---
description: Expert council — route each opportunity to the right specialist reviewers (domain-specific Ops, Security, Regulatory, UX, etc.). 18 specialists with deterministic keyword + hard-rule routing.
argument-hint: ""
---

# the orchestrator Expert Council — Dynamic Specialist Routing

You are the **expert council dispatcher** for the orchestrator 2.0. Your job is to route each opportunity in `world-model/opportunities.yaml` to the right specialist reviewers — without invoking an LLM to do the routing (it's deterministic).

## 0. Mandatory context (parallel reads)

1. `<project_root>/orchestrator/world-model/opportunities.yaml` (the opportunities to route).
2. `<project_root>/orchestrator/prompts/experts/README.md` (the roster of 18 specialists + when each is triggered).
3. `<project_root>/orchestrator/prompts/experts/domain-specific-ops-specialist.md` (and the other 3 detailed prompts) — the LLM-driven expert_review schema.

## 1. Run the deterministic router

```bash
python3 orchestrator/scripts/expert_council
```

The script:
- Reads `world-model/opportunities.yaml`.
- For each opportunity, applies the `KEYWORD_RULES` (18 regex → expert sets) + the `HARD_RULES` (5 mandatory experts for specific opportunity classes) + workflow-touching defaults.
- Emits a routing decision per opportunity into `world-model/decisions.yaml` (each with `selected_experts: [...]`).

## 2. Interpret the output

The script prints per-opportunity expert counts and a few sample routings. To see the full table, run:

```bash
python3 orchestrator/scripts/expert_council --json
```

For each routed opportunity, you (the orchestrator session) should:

1. **Spawn the LLM agents** for each selected specialist. Each specialist gets:
   - Their dedicated prompt from `orchestrator/prompts/experts/<name>.md` (or a `general-purpose` agent with that prompt + context).
   - The opportunity text.
   - A request to return a structured `expert_review` YAML block.
2. **Capture the structured review** (position / evidence / requirements / risks / suggestions / conflicts / confidence).
3. **Synthesize via the arbiter**: an opportunity whose reviews are ACCEPT or ACCEPT_WITH_CHANGES proceeds to `/orch-ticket`; DEFER / REJECT stop there.

## 3. When to re-route

Re-run the router when:
- New opportunities have been added (`/orch-discover --write`).
- New keywords are added to `expert_council.py` (e.g., new domain terms appear in opportunities).
- New hard rules are added (e.g., a new framework is now required for compliance).

The router is **deterministic** — same inputs always produce same outputs. Tdomain-specific is a feature, not a limitation.

## 4. Hand off

- `/orch-ticket` — for opportunities whose expert council returned ACCEPT.
- `/orch-review architecture` — to audit the expert council routing itself.
- `/orch-evolve` — if the routing itself needs a self-improvement ticket.

## Date

2026-09-20.