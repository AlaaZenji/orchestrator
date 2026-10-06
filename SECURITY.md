# Security Policy & Architecture

## Supported Versions

| Version | Supported | Status |
| :--- | :---: | :--- |
| **0.1.x** | ✅ | **Active Reference Standard** |

---

## Security Architecture Principles

The Orchestrator Platform enforces security boundaries via **deterministic operating system controls, cryptographic identities, and out-of-band policy engines**, based on the axiom that **prompt engineering is not a security boundary**:

1. **Zero-Trust Agent Sandbox:**
   Agent processes execute inside quarantined environments (Linux bubblewrap, containers, or dedicated ephemeral directories). The agent has zero direct access to orchestrator control plane credentials or host filesystems.
2. **Credential Brokering & Secret Isolation:**
   Production secrets, database credentials, and cloud API keys are **never injected into the worker sandbox**. All external authenticated requests route through an outbound broker.
3. **Storage-Enforced Monotonic Fencing:**
   Monotonically increasing sequence tokens guarantee that stale or compromised workers cannot overwrite legitimate work after their lease expires.
4. **Actor Role Authorization:**
   State transitions are strictly authenticated by caller role (`HUMAN_OPERATOR`, `AUTHORIZED_VERIFIER`, `AGENT_WORKER`). An agent cannot self-complete or self-approve work.
5. **Path Traversal Defenses:**
   Tool gateways strictly validate that all relative paths resolve within the assigned ephemeral workspace root (`resolve_safe_path`).

---

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately:
- **Email:** alaa.zingi.2004@gmail.com
- **Subject Prefix:** `[SECURITY] Orchestrator Platform`

Please do **not** disclose vulnerabilities in public GitHub issues.

### Response Timeline:
- **Initial Acknowledgment:** Within 48 hours.
- **Triage & Assessment:** Within 5 business days.
- **Patch Deployment:** Critical issues patched within 14 days.
