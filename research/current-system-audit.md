# Forensic Architectural Audit of Current System (AlaaZenji/orchestrator)

## 1. Executive Summary & Audit Scope

We performed a comprehensive forensic audit of the `AlaaZenji/orchestrator` repository (commit `180d405`, ~12,100 lines of Python code, SQL migrations, markdown templates, and tests).

The repository contains valuable conceptual insights (notably the realization that distributed locking requires monotonic fencing tokens and an outbox pattern). However, the implementation is **severely compromised by architectural contradictions, domain leakage, dual sources of truth, and uninsulated subprocess execution**.

Most glaringly, running the existing test suite (`pytest tests/`) immediately fails:
`tests/test_smoke.py::test_no_project_specific_content_in_repo` fails because domain-specific terms from an external clinical healthcare system (`clinical`, `ASTRA_DB_ROLE`, `ASTRA_APP_PGPASSWORD`, `ASTRA_DB_TENANT`, `TKT-ORCH-`, etc.) are hardcoded directly inside `lease.py`, `cli.py`, `state.py`, and runtime templates.

---

## 2. Architecture As-Built

The current system operates not as a structured library or daemon, but as a **script generator and copy-paster**:
- `src/orchestrator_runtime/cli.py` (`orchestrator-setup`) copies 20+ standalone Python scripts from `templates/scripts/` into a target project's `orchestrator/scripts/` folder.
- The scripts communicate through a mixture of:
  1. A relational database (Postgres or SQLite) containing `orchestrator_lease` and `orchestrator_outbox`.
  2. Local Markdown ticket files in `.tickets/` with YAML frontmatter.
  3. Ad-hoc JSON state files and locks created via `fcntl.flock`.
  4. Local bash commands invoking Claude Code or subagents.

---

## 3. What Is Good (Concepts Worth Preserving)

1. **Recognition of Martin Kleppmann's Fencing Tokens:** The codebase correctly identifies (in `V001__lease.sql` and `lease.py`) that UUID-based locks fail under process pauses, and that a monotonically increasing integer (`BIGSERIAL`) is required.
2. **Transactional Outbox Pattern:** The inclusion of an outbox table (`orchestrator_outbox`) and consumer acknowledges the dual-write problem when updating state and emitting events.
3. **Anti-Stall & Heartbeat Awareness:** The anti-stall prompt template and watchdog loops correctly identify that LLM agents frequently hang on tool calls or enter silent stalls.
4. **DAG Dependency Awareness:** Scripts like `dag_optimizer.py` and `burn_queue.py` attempt to schedule work respecting ticket dependency graphs.

---

## 4. Fundamental Flaws & Architectural Contradictions

### 4.1 Fatal Contradiction: Multiple Unsynchronized Sources of Truth
The orchestrator maintains three competing sources of authoritative state:
1. **Markdown Ticket Frontmatter:** `status: QUEUED`, `claimed_by: agent-1` inside `.tickets/TKT-xxx.md`.
2. **Database Tables:** `orchestrator_lease` table with `holder_id`, `fencing_token`, and `lease_expires_at`.
3. **Filesystem JSON/Fcntl State:** `auto_reconcile.py` attempts to rebuild state by reading markdown files and running `fcntl.flock` on local locks.

*Consequence:* A ticket can be marked `COMPLETED` in the database, while remaining `QUEUED` in its markdown file, while a worker script crashes. `auto_reconcile.py` attempts heuristic parsing to resolve conflicts, leading to race conditions where stale file edits overwrite database truths.

### 4.2 Leases Without Resource-Side Fencing
While `V001__lease.sql` generates a monotonic `fencing_token`, **the workers and filesystem tools never check this token during execution**:
- In `lease.py` and `dispatch.py`, the agent executes arbitrary shell commands modifying files directly on the host machine.
- If Worker A's lease expires and Worker B is granted the lease with a higher token, Worker A continues running its bash commands and writing files to the shared filesystem!
- *Verdict:* The fencing token is generated in the DB, but is a **no-op** in practice because the filesystem and git mutations are not guarded by any fenced gateway.

### 4.3 Severe Domain Leakage (Failing Smoke Tests)
Inspection of `src/orchestrator_runtime/templates/scripts/lease.py`:
- Hardcoded clinical references: `clinical`, `ASTRA_DB_ROLE`, `ASTRA_APP_PGPASSWORD`, `ASTRA_DB_TENANT`.
- Ticket prefixes hardcoded: `TKT-P0-`, `TKT-ORCH-`, `TKT-DEEP-`.
- *Verdict:* The repository was created by hastily copy-pasting code from an internal proprietary healthcare AI project ("Astra" / "World Intelligence OS") without generic abstraction.

### 4.4 Fragile Code Generation / Template Copying Model
The architecture distributes orchestration by copy-pasting 20 raw `.py` scripts into client repositories.
- Upgrades require overwriting client scripts, risking loss of local modifications.
- There is no clean Python API (`import orchestrator`), no clean CLI for direct execution, and no formal plugin architecture.

### 4.5 Security Vulnerabilities: Unsandboxed Shell Execution
In `dispatch.py` and `burnqueue_all.py`:
- Agent commands are executed directly via `subprocess.run(shell=True)` on the host operating system.
- An agent hallucinating or compromised via prompt injection has full read/write access to the developer's laptop, home directory, SSH keys, and environment variables.

---

## 5. Comprehensive Capability Inventory & Migration Matrix

| Existing Subsystem / Script | Lines | Current Function | Fundamental Problem | Target Classification |
| :--- | :--- | :--- | :--- | :--- |
| `cli.py` (`orchestrator-setup`) | 803 | Copies scripts & slash commands | Brittle file copier; no runtime engine | **REPLACE** (Provide real CLI & Python SDK) |
| `lease.py` | 1334 | Manages DB lease claims | Monolithic; mixes DB logic with file I/O; domain leaks | **REPLACE WITH CORE** (Clean `reliability/leases`) |
| `state.py` | 855 | Ticket state transitions | Dual source of truth with markdown files | **REPLACE WITH CORE** (Clean `orchestration/state_machine`) |
| `outbox.py` & `outbox_consumer.py`| 1170 | Transactional outbox pattern | Polling loop; tight coupling to ticket format | **REFACTOR TO CORE** (Clean `events/outbox` with CloudEvents) |
| `auto_reconcile.py` | 837 | Heuristic repair of split-brain | Symptom patch for dual source of truth | **REMOVE / REPLACE** (State machine recovery loop) |
| `burn_queue.py` & `burnqueue_all.py`| 592 | Multi-worker queue processor | Hardcoded to Claude Code CLI and markdown files | **REPLACE WITH CORE SCHEDULER** (`orchestration/scheduler`) |
| `watchdog.py` & `watchdog_loop.py` | 134 | Heartbeat stall detector | Shell loop checking timestamps | **REPLACE WITH CORE** (`reliability/watchdog`) |
| `dag_optimizer.py` | 390 | Resolves ticket dependencies | Custom topological sort on markdown files | **REPLACE WITH CORE** (`orchestration/dependency`) |
| `verdict_conflict.py` & `verifier_doctrine_gate.py` | 1680 | Heuristic verification rules | Highly domain-specific; ad-hoc rules | **REFACTOR TO PLUGIN** (`verification/plugins`) |
| `bottleneck_detector.py` | 337 | Performance statistics | Ad-hoc script | **REPLACE WITH CORE METRICS** (OpenTelemetry) |

---

## 6. Audit Verdict

The existing repository cannot be salvaged by incremental patching.
- The dual source of truth (Markdown vs DB) must be abolished. The database must be the **sole authoritative state store**.
- The script-copying pattern must be replaced by a **production-grade Python library, CLI, and HTTP control plane**.
- Fencing must be extended from an inert database column to an active **Fenced Gateway** protecting sandboxed workspaces and git branches.
- Domain-specific terms and clinical leakage must be completely purged.
