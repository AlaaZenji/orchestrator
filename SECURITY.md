# Security Policy

## Supported Versions

orchestrator-runtime is currently maintained with security updates for:

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | ✅ Active          |
| < 0.1   | ❌ End-of-life     |

## Reporting a Vulnerability

If you discover a security vulnerability in orchestrator-runtime, please report it privately:

- **Email:** alaa.zingi.2004@gmail.com
- **PGP key:** (request via email)
- **Subject prefix:** `[SECURITY] orchestrator-runtime`

Please **do not** file a public GitHub issue for security vulnerabilities.

## What to include

When reporting a vulnerability, please include:

1. Description of the vulnerability
2. Steps to reproduce
3. Affected versions
4. Potential impact
5. Suggested fix (if any)

## Response time

- **Acknowledgment:** within 48 hours
- **Initial assessment:** within 1 week
- **Patch for critical issues:** within 2 weeks
- **Patch for non-critical issues:** next minor release

## Security architecture

The orchestrator itself is designed with security in mind:

- **Zero runtime dependencies** — smaller attack surface
- **Stdlib-only core** — no third-party code in the hot path
- **Kleppmann monotonic fencing tokens** — prevents stale-claim attacks
- **Tenant isolation via Postgres RLS** — when multi-tenant mode is enabled
- **Canonical NULLIF GUC pattern** — defensive against unset tenant context
- **Idempotent outbox** — prevents duplicate state mutations
- **No autonomous irreversible actions** — every operator action is human-approved

## Out of scope

The orchestrator does NOT include:

- Authentication or authorization (your project's concern)
- Encryption at rest or in transit (your project's concern)
- Audit logging (your project's concern — though the orchestrator's outbox pattern can be wired into your audit log)
- Network policies or rate limiting

The orchestrator is a **coordination primitive**. Security is delegated to your application.
