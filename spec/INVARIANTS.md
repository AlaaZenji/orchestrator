# Formal System Invariants

This specification defines the non-negotiable invariants of the durable agent orchestrator. Any implementation claiming compliance with this specification MUST pass conformance test suites validating every invariant below.

---

### INV-001: Single Active Execution Mutual Exclusion
> **A Work entity cannot have two active executions unless explicitly configured for parallel fan-out.**
- *Formal Statement:* At any instant $t$, $\lnot \text{allow\_parallel} \implies |\{e \in \text{Executions}(w) \mid e.\text{status} = \text{RUNNING}\}| \le 1$.
- *Enforcement:* Enforced at storage layer via unique active lease per Work.

### INV-002: Stale Worker Fencing
> **A stale execution cannot mutate protected authoritative state or promote candidate artifacts.**
- *Formal Statement:* Let $z_{\text{active}}$ be the highest fencing token issued for $w$. If worker presents $z < z_{\text{active}}$, any state mutation or artifact promotion MUST be rejected.
- *Enforcement:* Storage-side verification `WHERE fencing_token = :claimed_token`.

### INV-003: Total Event Auditability
> **Every durable state transition produces an immutable domain event.**
- *Formal Statement:* $\forall \Delta S \in \text{StateTransitions}, \exists e \in \text{Events}$ such that $e$ is persisted atomically with $\Delta S$.
- *Enforcement:* Transactional Outbox pattern committed in the same database transaction.

### INV-004: Agent Cannot Self-Complete Work
> **An agent process cannot directly transition a Work entity into `SUCCEEDED` status.**
- *Formal Statement:* $\text{Transition}(w, \text{SUCCEEDED})$ requires $\text{ActorRole} \in \{\text{VERIFIER}, \text{HUMAN\_OPERATOR}, \text{SUPERVISOR\_POLICY}\}$. An actor with $\text{ActorRole} = \text{AGENT\_WORKER}$ is strictly prohibited.
- *Enforcement:* Guard check inside `validate_transition()`.

### INV-005: Distinct Retry Executions
> **Retries produce distinct, causally linked execution attempts.**
- *Formal Statement:* For a given $w$, attempt $k+1$ creates a new execution record $e_{k+1}$ with $\text{attempt} = k+1$ and $z_{k+1} > z_k$. The failed execution $e_k$ remains immutable in historical records.
- *Enforcement:* Execution table primary key is `(work_id, attempt)` or unique `execution_id`.

### INV-006: External Side-Effect Idempotency
> **Every state-mutating operation accepts an idempotency key.**
- *Formal Statement:* Submitting operation $O$ with idempotency key $K$ twice yields identical state and return values without duplicating side effects.
- *Enforcement:* `orchestrator_idempotency` table with unique constraint on $K$.

### INV-007: Crash Reconstruction from Persistent State
> **The orchestrator can reconstruct consistent cluster state solely from the persistent database WAL after a sudden crash.**
- *Formal Statement:* Following crash of all orchestrator instances, a fresh orchestrator process running a reconciliation sweep will restore all orphaned works to valid, recoverable states without manual intervention.
- *Enforcement:* Reconciler sweeps active leases where `expires_at < CURRENT_TIMESTAMP`.

### INV-008: Provider-Neutral Runtime Abstraction
> **The orchestration control plane contains zero vendor-specific imports or prompt logic.**
- *Formal Statement:* The core engine depends only on the abstract `AgentRuntime` interface. Claude, Codex, Gemini, or local models are swappable runtime adapters.
- *Enforcement:* Lint and CI rule banning provider imports in `src/domain/` and `src/orchestration/`.

### INV-009: Resource Isolation & Workspace Quarantine
> **An execution cannot mutate shared resources directly prior to verification.**
- *Formal Statement:* All agent filesystem and git operations during execution occur inside isolated sandboxes or ephemeral topic branches. Promotion to shared branches requires verification.
- *Enforcement:* Sandbox provider and git staging architecture.

### INV-010: Eventual Cancellation Observability
> **When a Work is cancelled, its active execution is terminated within a bounded grace period.**
- *Formal Statement:* $\forall w \in \text{Works}$, if $w.\text{status} = \text{CANCELLED}$ at $t_0$, the associated worker process is sent SIGTERM at $t_0$ and SIGKILL at $t_0 + \Delta t_{\text{grace}}$.
- *Enforcement:* Process supervisor and watchdog loop.

### INV-011: Bounded Recovery Work
> **System recovery after failure cannot produce unbounded duplicate executions.**
- *Formal Statement:* Total execution attempts for work $w$ across all recoveries and retries cannot exceed $w.\text{max\_retries} + 1$.
- *Enforcement:* `retry_count` is monotonically incremented in durable storage.

### INV-012: Out-of-Band Authorization Evaluation
> **Authorization is evaluated by deterministic policy outside the agent's prompt context.**
- *Formal Statement:* Policy decisions evaluating whether a tool call or transition is allowed rely strictly on deterministic rules, role attributes, and cryptographic signatures, never on natural language justifications.
- *Enforcement:* Policy engine layer.
