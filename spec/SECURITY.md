# Security Specification

## 1. Scope & Objective

This specification formalizes the security policies, isolation boundaries, credential management rules, and authorization gates for the orchestrator.

---

## 2. Hard Security Rules

1. **Rule 1 (The Zero-Trust Agent Rule):** Code running inside an agent worker process or sandbox is treated as untrusted. The agent cannot grant itself permissions, approve its own work, or access orchestrator control plane credentials.
2. **Rule 2 (No Secrets in Sandbox):** Real production API tokens, database passwords, and cloud credentials MUST NEVER be injected into the worker environment. All external authenticated requests route through a credential brokering proxy.
3. **Rule 3 (Filesystem Quarantine):** Workers execute inside an isolated directory. The host root `/`, user home directory `~`, and parent folders are strictly read-only or inaccessible.
4. **Rule 4 (Network Egress Control):** Network access from worker sandboxes is restricted to configured domain allowlists (e.g. PyPI, npm, GitHub) or disabled entirely by default.
5. **Rule 5 (Tamper-Evident Audit):** All tool invocations and state changes are recorded in an append-only audit log with SHA-256 content addressing.

---

## 3. Actor Roles & Permissions Matrix

```python
class ActorRole(str, Enum):
    ANONYMOUS = "anonymous"
    AGENT_WORKER = "agent_worker"
    HUMAN_OPERATOR = "human_operator"
    AUTHORIZED_VERIFIER = "authorized_verifier"
    SYSTEM_RECONCILER = "system_reconciler"
```

| Operation | Agent Worker | Human Operator | Authorized Verifier | System Reconciler |
| :--- | :---: | :---: | :---: | :---: |
| `CreateWork` | ALLOW (if enabled)| ALLOW | ALLOW | ALLOW |
| `ClaimLease` | ALLOW | DENY | DENY | DENY |
| `Heartbeat` | ALLOW | DENY | DENY | DENY |
| `SubmitVerification` | ALLOW | ALLOW | DENY | DENY |
| `RecordVerificationVerdict`| **DENY** | ALLOW | **ALLOW** | DENY |
| `ApproveWork` | **DENY** | **ALLOW** | DENY | DENY |
| `CancelWork` | **DENY** | **ALLOW** | DENY | **ALLOW** |
| `ReapExpiredLease` | **DENY** | ALLOW | DENY | **ALLOW** |

---

## 4. Sandbox Provider Interface

```python
class SandboxProvider(ABC):
    """Abstract interface for executing untrusted agent code."""

    @abstractmethod
    async def create_workspace(self, work_id: str, execution_id: str) -> Path:
        """Provisions an isolated, ephemeral workspace directory."""
        ...

    @abstractmethod
    async def execute_command(
        self,
        workspace_path: Path,
        command: list[str],
        env: dict[str, str],
        timeout_seconds: float = 60.0
    ) -> CommandResult:
        """Executes a command within the quarantined sandbox boundary."""
        ...

    @abstractmethod
    async def destroy_workspace(self, workspace_path: Path) -> None:
        """Securely deletes the ephemeral workspace and cleans up mounts."""
        ...
```
