---
description: Estimate time-to-finish for orchestrator-side burn + user-side gates with deadline breakdown
---

# /orch-timer — Estimated Completion Timer

Estimate time-to-finish for orchestrator-side burn + user-side gates.

Tdomain-specific command runs `python3 orchestrator/scripts/estimated_completion.py` and prints:
- DONE so far / REMAINING burnable / BLOCKED on user gates
- AI runtime estimate (optimistic / expected / pessimistic bounds)
- YOUR time estimate for user-team gates (sequential breakdown)
- External gates that block progress

100% accuracy is **impossible** — estimates are from observed burn rate + per-ticket overhead + stall rate. Actual time depends on worker stalls + laptop resources + your gate-closure speed.

## Usage

```
/orch-timer          # human-readable output
/orch-timer --json    # JSON for programmatic consumption
```

## Output Format

```
======================================================================
ASTRA ESTIMATED COMPLETION TIMER
Generated: 2026-09-25T18:00:00Z
======================================================================

DONE so far:                    162
REMAINING burnable tickets:     28
BLOCKED on user/external gates:  2
Current burn rate:              3.0 tickets/hour

AI RUNTIME ESTIMATE (orchestrator-side closure):
  Optimistic (no stalls):       2.5 hours
  Expected (20% stall rate):    3.5 hours
  Pessimistic (every stall):    4.5 hours

YOUR TIME ESTIMATE (user-team gates — sequential):
  Total:                        19.0 hours
    - BLOCKER-2 (cloud account): 2.0 hours
    - BLOCKER-4 (NSSF Lebanon docs): 10.0 hours
    - Pilot discovery call (prep + call): 2.0 hours
    - CDS external validation sign-offs (V2/V3): 5.0 hours

EXTERNAL GATES (you must close these):
  - BLOCKER-2: cloud account (provisioning)
  - BLOCKER-4: NSSF Lebanon docs (acquisition)
  - Pilot discovery call scheduling
```

## Implementation

Script: `orchestrator/scripts/estimated_completion.py`

Tunable constants:
- `MIN_PER_P0 = 60` — avg minutes per P0 ticket
- `STALL_RATE = 0.20` — fraction of dispatches that get re-dispatched
- `STALL_RESCUE_MIN = 35` — overhead per stall re-dispatch
- `TICKET_OVERHEAD_MIN = 5` — lease, verifier, cascade per ticket

User-gate breakdown (your hours):
- BLOCKER-2 cloud account: 2 hours
- BLOCKER-4 NSSF Lebanon docs: 10 hours
- Pilot discovery call: 2 hours
- CDS external validation sign-offs: 5 hours
- **Total your time: ~19 hours**

## What Tdomain-specific Cannot Predict

- Worker stream-watchdog stalls (can add 30-45 min per stall)
- Laptop CPU/RAM ceiling (can't exceed 3-4 worker fan-out)
- Sub-agent tool call latency (network roundtrip per Read/Edit/Write)
- Clinical validation findings from CDS external validation reports
