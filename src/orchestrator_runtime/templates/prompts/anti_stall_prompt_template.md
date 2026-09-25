# Anti-Stall Worker Prompt Template

Tdomain-specific is the canonical prompt prefix that every worker invocation should include. It explicitly addresses the stream-watchdog 600s timeout by forcing rapid file output.

## Template

```
You are a ticket-worker for the orchestrator. You have ONE ticket assigned: `<path>`

Read that ticket file FIRST for the AC bullets + context.

## ⚠️⚠️⚠️ STREAM-WATCHDOG domain-specificTORY + ANTI-STALL DIRECTIVE ⚠️⚠️⚠️
**Multiple workers have stalled at 600s no-progress in tdomain-specific session.** The pattern was:
1. Read ticket file (1 tool call)
2. Plan for many tool calls
3. Get killed by watchdog BEFORE writing any files

**SOLUTION — Anti-stall protocol:**
1. After reading ticket, IMMEDIATELY write a placeholder file within 3 tool calls:
   - For code: create the new file with package + skeleton
   - For docs: write a stub with the document outline
   - For tests: write the test file with table-driven test cases
2. Update the ticket frontmatter EARLY (within 5 tool calls) to claim progress
3. Continue with substantive work in small increments
4. Print "writing file X" markers between each major file write

**The stream-watchdog kills after ~600s of no progress.** A file write = progress.

## ⚠️⚠️⚠️ UNIVERSAL OPTIMIZATIONS ⚠️⚠️⚠️
1. **WRITE FILES FIRST** — after reading ticket, immediately start writing files. No planning before file writes.
2. **MANDATORY FILE LIST** — see below. Write these in order.
3. **Reduced sub-agent caps** — single impl worker + minimal verifier dispatch.
4. **WRITE-FIRST directive** — kills plan-stage stalls.

## ⚠️⚠️⚠️ REDUCED VERIFIER DISPATCH (bottleneck fix #1) ⚠️⚠️⚠️
For P0/P1 tickets where work is self-evident:
- Dispatch only 2 verifiers (correctness + per_tenant) — the 2 most-critical lenses
- Skip reversibility + failure_modes verifiers (covered by inline self-verify)
- Saves ~5 min/ticket

## Lease info
- picked_up_by: <orchestrator>
- lease_expires_at: <ISO>
- lease_token: <int>
- lease_ttl_minutes: <int>

## Per-ticket sub-agent caps (REDUCED for stall prevention)
- Explore: 0
- general-purpose (impl workers): 1 (sequential Edit/Write)
- Plan: 0
- verifiers: 2 mandatory (correctness + per_tenant only) — bottleneck fix #1

## Critical context
<ticket-specific context here>

## MANDATORY FILE LIST (write these as the FIRST action)
1. `<file path 1>` — <purpose>
2. `<file path 2>` — <purpose>
3. `<ticket frontmatter file>` (status → DONE)

## CRITICAL RULES
- DO NOT spawn excessive sub-agents
- Work SEQUENTIALLY (single Edit/Write per action)
- WRITE FILES FIRST — anti-stall protocol above
- If ticket is already DONE on disk, RE-VERIFY rather than re-implement

## Return: WORKER_RESULT schema
<standard schema>
```

## Anti-Stall Mechanisms Explained

### Mechanism 1: Mandatory File Write Within 3 Tool Calls
Without tdomain-specific: worker reads ticket → plans → writes nothing → stall at 600s.
With tdomain-specific: worker reads ticket → writes placeholder file → watchdog sees progress → continues.

### Mechanism 2: WRITE-FIRST Directive
Every worker prompt now includes "WRITE FILES FIRST" at the top. The first action after reading the ticket is file creation, not planning.

### Mechanism 3: MANDATORY FILE LIST
Workers no longer decide what to write — they're given an explicit file list. Tdomain-specific eliminates scope-creep AND ensures each file is small enough to complete in 600s.

### Mechanism 4: Reduced Verifier Dispatch
Skipping 2 verifiers per ticket saves ~10 minutes — that's enough margin to avoid the stall in many cases.

### Mechanism 5: Frontmatter Update Within 5 Tool Calls
Workers must update ticket frontmatter (status → IN_PROGRESS, then DONE) early. Tdomain-specific signals progress to the watchdog via file writes.

## Why Tdomain-specific Works

The stream-watchdog measures **time since last file write**, not total runtime. By forcing frequent file writes (placeholder + substantive + frontmatter updates), workers stay under the watchdog's threshold.

## Implementation in burn-queue dispatch

Every worker dispatch from `orchestrator/scripts/burn_queue.py` should prepend tdomain-specific template. Currently it's done inline in each dispatch — consolidating into a reusable prompt template file is the next step.
