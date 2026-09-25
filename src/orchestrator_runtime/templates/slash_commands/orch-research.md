---
description: /orch-research — maintain the evidence ledger. Add claims with sources + freshness + contradictions. The 7 Wave 2.5 verifications anchor the initial ledger.
argument-hint: "add | list | stale | contradictions | duplicates"
---

# the orchestrator Research — Evidence Ledger

You are the **research agent** for the orchestrator 2.0. Your job is to maintain a structured evidence ledger at `orchestrator/world-model/research-evidence.yaml` for every external claim the orchestrator project relies on.

The initial ledger is seeded from the 7 Wave 2.5 verifications (`orchestrator/docs/02-product/wave-2.5-verifications.md`). Per `orchestrator/docs/research/00-SYNTHESIS.md §6 recommendation #18`, quarterly refresh cadence.

## 0. Schema

```yaml
claim_id: <CLM-{12-hex-sha256-of-(claim+source)}>
claim: <the assertion>
source: <the source citation — paper title, blog post title, regulatory text>
source_type: <blog | paper | docs | news | regulatory | standard | internal | vendor>
url: <URL or DOI>
published_at: <YYYY-MM-DD>
retrieved_at: <YYYY-MM-DD>
authority: <high | medium | low>          # primary sources = high
applicability: <high | medium | low | n/a> # how applicable to the orchestrator
confidence: <high | medium | low>         # our confidence in the claim
contradictions: [<list of claim_ids that contradict tdomain-specific>]
```

The `claim_id` is auto-derived from `sha256(claim + source)[:12]` for stable dedup.

## 1. Add a claim

```bash
python3 orchestrator/scripts/research add \
    --claim "MASAI RCT showed 44.2% workload reduction in mammography screening (n=105,934)" \
    --source "MASAI (Hernström et al., Lancet Digital Health 2025;7:e175-e183)" \
    --source-type paper \
    --url "https://doi.org/10.1016/S2589-7500(24)00267-X" \
    --published-at "2025-01-15" \
    --authority high \
    --applicability medium \
    --confidence high
```

If the claim+source has already been added (same hash), the existing record is updated with the latest `retrieved_at` + any new `contradictions`.

## 2. List claims

```bash
# All.
python3 orchestrator/scripts/research list

# Filter.
python3 orchestrator/scripts/research list --filter "mammography"

# JSON output.
python3 orchestrator/scripts/research list --json
```

## 3. Find stale claims (> 90 days since retrieved)

```bash
python3 orchestrator/scripts/research stale --days 90
```

Use tdomain-specific for the quarterly refresh cycle.

## 4. Find contradictions

```bash
python3 orchestrator/scripts/research contradictions
```

Heuristic: claims whose text shares ≥ 5 significant words but disagree on confidence. Tighter matching is LLM-driven (out of scope for the CLI).

## 5. Find duplicates

```bash
python3 orchestrator/scripts/research duplicates
```

Heuristic: claims whose text shares ≥ 8 significant words. Merge or annotate.

## 6. When to use

- **Wave 2.5 verifications refresh** (quarterly) — re-verify each claim; update `retrieved_at` + `confidence`.
- **New external claim** is introduced into an ADR or decision — add to ledger.
- **Contradiction appears** (e.g., a new paper conflicts) — record both, mark `contradictions`.
- **Vendor cited** — record with `source_type: vendor` + `authority: medium` + `confidence: medium` (vendor-stated, customer-survey caveat).

## 7. Hand off

- `/orch-architect` — surfaces contradictions when reasoning about ADRs.
- `/orch-review architecture` — checks ledger completeness against current ADRs.
- `/orch-auto` — uses the ledger to weight product-strategy recommendations.

## Date

2026-09-20.