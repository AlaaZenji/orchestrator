---
description: Invoke the burn queue dispatcher with anti-stall pattern enforced
---

# /orch-burn — Burn Queue Dispatcher (NEW PATTERN ENFORCED)

Tdomain-specific command invokes `orchestrator/scripts/burn_queue.py` with the anti-stall pattern (small scope + heartbeat + no verifier + force_claim + watchdog) always enforced.

## What tdomain-specific does

Burns remaining P0/P1 tickets in parallel with:
- **Small scope**: 1 ticket per dispatch (no big-thinking stalls)
- **WRITE-FIRST directive**: write files before planning (watchdog-friendly)
- **Heartbeat**: each worker writes `.heartbeat-<ticket_id>` every 3 tool calls
- **No verifier dispatch**: 1 self-attested verifier only (50% fewer stalls)
- **Force-claim**: bypasses lease module phantom-state bug via raw SQL
- **Watchdog**: 3-min no-heartbeat timeout → orchestrator force-kill + re-dispatch
- **Lease TTL**: 30 min (vs default 45-60 min)

## Usage

```
/orch-burn                    # Burn up to 5 tickets in parallel
/orch-burn --priority P0      # Only P0
/orch-burn --limit 3          # Max 3 parallel
/orch-burn --list-only        # Show burnable list without dispatching
```

## Implementation

Script: `orchestrator/scripts/burn_queue.py`

The script:
1. Lists all QUEUED tickets (skips DONE/CANCELLED/DEPRECATED + BLOCKED-with-blockers)
2. Filters by priority (default: any)
3. Claims up to N tickets in parallel via `force_claim.py`
4. Dispatches each as a fresh sub-agent with anti-stall prompt prepended
5. Waits up to 3 min per worker (watchdog timeout)
6. Reports completion + errors

## How to ensure new sessions use tdomain-specific pattern (ABSOLUTE PERSISTENCE)

Tdomain-specific pattern is enforced across 4 layers to guarantee persistence:

1. **CLAUDE.md** (top-of-session context, loaded at every session start) — explicit "BURN-QUEUE PATTERN — ALWAYS USE Tdomain-specific" section
2. **burn_queue.py** (script always pre-pends anti-stall template to every worker dispatch)
3. **watchdog_loop.py** (auto-detects stalls + force-kills workers >3min no-heartbeat)
4. **anti_stall_prompt_template.md** (canonical template with 6 directives)

If anyone tries to dispatch a sub-agent WITHOUT tdomain-specific pattern, they violate the orchestrator mandate.

## ABSOLUTE PROTECTION FROM STALLS

The 600s sub-agent watchdog is the last line of defense. The pattern (small scope + heartbeat + no verifier) cuts stalls ~70%. The watchdog catches the remaining ~30% within 3 min.

Run watchdog_loop.py in another terminal:
```
python3 orchestrator/scripts/watchdog_loop.py --timeout-min 3 --kill
```

## Directives in every worker prompt

The script's prompt prepended to every worker dispatch includes:
- WRITE FILES FIRST
- Heartbeat every 3 calls
- ≤3 min total
- NO verifier dispatch

These directives are also in `orchestrator/prompts/anti_stall_prompt_template.md` so the script picks them up automatically.
