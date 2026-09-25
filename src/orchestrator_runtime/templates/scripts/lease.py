"""Postgres-backed monotonic fencing-token lease (TKT-NNN).

The orchestrator's distributed coordination primitive (per
``orchestrator/CONVENTIONS.md §Ticket lease lock`` + Kleppmann 2016,
"How to do distributed locking"). Replaces the previous UUID-v4
implementation: a UUID has no ordering, so the old guard comment could
not implement the Kleppmann monotonic-fencing-token argument. The new
implementation uses a Postgres table with a ``BIGSERIAL fencing_token``
column — every claim, heartbeat, and release bumps the token, so a
stale holder's UPDATE is rejected at the storage layer with zero rows
affected.

Public API (the four operations every caller needs):

    * ``claim(ticket_id, tenant_id, holder_id, ttl_minutes) -> int``
      — atomic insert. Returns the new ``fencing_token`` (a positive
      integer, strictly monotonically increasing across the cluster).
      Raises :class:`LeaseHeldError` if the ticket is already held by a
      non-expired lease.

    * ``heartbeat(ticket_id, tenant_id, holder_id, fencing_token,
      ttl_minutes) -> int``
      — extends ``lease_expires_at`` and rotates ``fencing_token`` if
      the supplied ``fencing_token`` is still the current maximum.
      Returns the new token. Raises :class:`StaleFencingTokenError` if
      a heartbeat / claim has since advanced past the supplied token.
      ``tenant_id`` is required because AC #2 mandates the
      ``app.current_tenant`` GUC be set BEFORE every query (the RLS
      policy would otherwise hide the row from the orchestrator_app role),
      and the only way to know which tenant owns the row without a
      prior read is to accept it from the caller.

    * ``release(ticket_id, tenant_id, holder_id, fencing_token) -> None``
      — deletes the row iff the supplied ``fencing_token`` is still
      current. Raises :class:`StaleFencingTokenError` otherwise (a stale
      release is the canonical "two holders tried to release the same
      lease" race). Same ``tenant_id`` rationale as ``heartbeat``.

    * ``read_lease(ticket_id, tenant_id) -> dict | None``
      — read-only convenience for the markdown-cache mirror described
      in AC #3. Returns the row as a dict, or ``None`` when no live
      lease exists for the given ticket. Sets the
      ``app.current_tenant`` GUC before the query (per AC #2).

    * ``reap_stale(now) -> int``
      — deletes every lease whose ``lease_expires_at < now``. Returns
      the count of rows deleted. Safe to run on a cron; idempotent.

The module sets the ``app.current_tenant`` GUC on every connection
BEFORE any query (per the project's decision records + ``the project's tenant-isolation config``). The
RLS policy on ``orchestrator.lease`` uses the canonical NULLIF form
(``tenant_id = NULLIF(current_setting('app.current_tenant', true),
'')::uuid``) — the GUC MUST be set in the same transaction as the
query, otherwise the policy returns zero rows. The module uses
``SET LOCAL`` (scoped to the transaction) so the value cannot leak
across connection-pool checkouts.

Stdlib only — uses :mod:`psycopg` 3.x which the project's Justfile
already pins (``the project's coding-standards doc`` + the
``psycopg[binary]`` driver already imported by ``tools/demo/``). All
DB I/O happens via the existing ``orchestrator_app`` least-privileged role
(per the project's decision records — ``orchestrator`` is superuser with BYPASSRLS, which would
defeat RLS testing).

Synthetic test data only (CLAUDE.md #3): no real tenant IDs, no real
ticket IDs. The module's tests (``tests/orchestrator/test_lease.py``)
use UUIDs from ``uuid.uuid5`` over a synthetic namespace.

Typical usage:

    from orchestrator.scripts.lease import claim, heartbeat, release

    token = claim("TKT-NNN", tenant_id, "agent-A", ttl_minutes=15)
    # ... do work ...
    token = heartbeat("TKT-NNN", "agent-A", token, ttl_minutes=15)
    # ... more work ...
    release("TKT-NNN", "agent-A", token)

Environment:

    ``DATABASE_URL``   — Postgres connection string (psycopg DSN). When
                        unset, the module falls back to the laptop-
                        staging default ``postgresql://orchestrator@localhost:${ORCHESTRATOR_DB_PORT:-5432}/orchestrator``. CI / cloud set this explicitly.

    ``ASTRA_DB_ROLE``  — Postgres role to connect as. Defaults to
                        ``orchestrator_app`` (the least-privileged role per
                        the project's decision records). Set to ``orchestrator`` only for ops
                        recovery; the module's RLS tests assume
                        ``orchestrator_app``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Final

# psycopg 3.x is the canonical Python driver for Postgres 16+ per CLAUDE.md
# + the project's coding-standards. Imported lazily at the call sites so the
# module loads even when DATABASE_URL is unset (the unit-test lane skips
# integration tests via the canonical pytest.skipif guard).
psycopg = None  # type: ignore[assignment]  # populated on first DB-touching call


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default database DSN when ``DATABASE_URL`` is unset. Matches the
#: laptop-staging bootstrap (memory: ``laptop-staging-bootstrap.md``,
#: Postgres on port int(os.environ.get("ORCHESTRATOR_DB_PORT", "5432")), auth ``trust`` on localhost).
_DEFAULT_DSN: Final[str] = "postgresql://orchestrator@localhost:${ORCHESTRATOR_DB_PORT:-5432}/orchestrator"

#: Default Postgres role. The least-privileged ``orchestrator_app`` role per
#: the project's decision records — every code path that touches domain-specific/audit/orchestrator
#: tables MUST be exercised as ``orchestrator_app``, never as ``orchestrator``
#: (which is superuser with BYPASSRLS, defeating RLS testing).
_DEFAULT_DB_ROLE: Final[str] = "orchestrator_app"

#: Default role password. Matches the local dev password set in
#: ``tools/scripts/the project's init-app-role recipe.sh`` (``orchestrator_app_dev``). CI sets
#: ``ASTRA_APP_PGPASSWORD`` explicitly.
_DEFAULT_DB_PASSWORD: Final[str] = "orchestrator_app_dev"

#: Maximum permitted TTL in minutes. Mirrors the ``CONVENTIONS.md
#: §Auto-derived lease_ttl_minutes`` cap (K8s ``Lease`` uses a
#: similar ``durationSeconds / 3`` heuristic).
_MAX_TTL_MINUTES: Final[int] = 60

#: Minimum permitted TTL in minutes. A sub-minute lease cannot reliably
#: outlive the heartbeat interval (every commit / 10 min, whichever is
#: sooner per ``CONVENTIONS.md §Ticket lease lock``).
_MIN_TTL_MINUTES: Final[int] = 1


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LeaseError(Exception):
    """Base class for every error raised by this module.

    Subclasses carry a structured ``code`` (UPPER_SNAKE_CASE) so the
    orchestrator cascade log can emit a precise marker (the same
    pattern as ``tests/orchestrator/test_verification_provenance.py
    VerifierProvenanceError``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LeaseHeldError(LeaseError):
    """Raised by :func:`claim` when the ticket is already held.

    Attributes:
        holder_id: The current holder's agent ID (so the caller can
            log who to escalate to).
        lease_expires_at: When the current lease expires (so the caller
            can decide to wait or move on).
    """

    code = "LEASE_HELD"

    def __init__(self, holder_id: str, lease_expires_at: datetime) -> None:
        super().__init__(
            code="LEASE_HELD",
            message=(
                f"ticket is already held by holder_id={holder_id!r} "
                f"until {lease_expires_at.isoformat()}"
            ),
        )
        self.holder_id = holder_id
        self.lease_expires_at = lease_expires_at


class StaleFencingTokenError(LeaseError):
    """Raised by :func:`heartbeat` / :func:`release` when the supplied
    ``fencing_token`` is no longer the current maximum.

    This is the canonical "Kleppmann stale-write rejection": a holder
    that paused (e.g., crashed, GC'd, network-partitioned) and resumed
    later MUST NOT be able to mutate state — the storage layer enforces
    this by rejecting the UPDATE when ``fencing_token`` has advanced.
    """

    code = "STALE_FENCING_TOKEN"

    def __init__(self, supplied_token: int, current_token: int | None) -> None:
        super().__init__(
            code="STALE_FENCING_TOKEN",
            message=(
                f"supplied fencing_token={supplied_token} is stale "
                f"(current={current_token}); the lease has rotated"
            ),
        )
        self.supplied_token = supplied_token
        self.current_token = current_token


class LeaseNotFoundError(LeaseError):
    """Raised when the row no longer exists (e.g., the reaper deleted a
    stale lease between the caller's read and the caller's write).
    """

    code = "LEASE_NOT_FOUND"

    def __init__(self, message: str = "no lease found") -> None:
        # Override the parent constructor — the code is fixed for this
        # error class, so callers should not be able to set it.
        super().__init__(code=self.code, message=message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _connect():  # pragma: no cover - thin wrapper, exercised via integration tests
    """Open a psycopg connection as the least-privileged ``orchestrator_app`` role.

    Resolves the DSN from ``DATABASE_URL`` (falls back to the laptop-
    staging default on port int(os.environ.get("ORCHESTRATOR_DB_PORT", "5432"))). The connection is opened with
    explicit ``user`` + ``password`` kwargs so we connect AS
    ``orchestrator_app`` directly — psycopg authenticates once, the session
    is in the role from the start, and ``SET ROLE`` is unnecessary.

    Per the project's decision records, ``orchestrator_app`` has no superuser + no BYPASSRLS, so
    RLS evaluates on every query (the test for the ``LEASED_ERROR`` /
    cross-tenant paths assumes this).

    The caller is responsible for the transaction lifecycle (use
    ``with conn:`` or explicit ``conn.commit()`` / ``conn.rollback()``).
    """
    # Lazy import so the module loads on machines without psycopg.
    global psycopg
    if psycopg is None:
        import psycopg as _psycopg  # type: ignore[no-redef,assignment]
        psycopg = _psycopg  # type: ignore[assignment]

    base_dsn = os.environ.get("DATABASE_URL", _DEFAULT_DSN)
    role = os.environ.get("ASTRA_DB_ROLE", _DEFAULT_DB_ROLE)
    # Local dev password matches ``the project's init-app-role recipe.sh``; CI sets
    # ``ASTRA_APP_PGPASSWORD`` explicitly. The ``orchestrator`` superuser
    # DSN (no password on trust) still works because we pass
    # ``user=role, password=password`` — psycopg replaces the
    # connection's role before sending the startup packet.
    password = os.environ.get("ASTRA_APP_PGPASSWORD", _DEFAULT_DB_PASSWORD)

    # ``autocommit=False`` so every operation is in one transaction
    # and ``SET LOCAL app.current_tenant`` scopes the GUC to that
    # transaction (per coding-standards §3.4).
    conn = psycopg.connect(  # type: ignore[arg-type]
        base_dsn,
        autocommit=False,
        user=role,
        password=password,
    )
    return conn


def _set_tenant(conn, tenant_id: str) -> None:
    """Set ``app.current_tenant`` on the current transaction.

    Uses ``SET LOCAL`` so the value cannot leak across connection-
    pool checkouts (per coding-standards §3.4). The value MUST be set
    in the same transaction as the query that depends on it (RLS is
    evaluated at query time, not at session start).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            (tenant_id,),
        )


def claim(
    ticket_id: str,
    tenant_id: str,
    holder_id: str,
    ttl_minutes: int,
) -> int:
    """Atomic lease claim — Kleppmann monotonic fencing token.

    Args:
        ticket_id: The ticket to claim (e.g., ``"TKT-NNN"``).
        tenant_id: The caller's tenant UUID (string form, parsed by
            Postgres ``::uuid`` cast in the RLS policy).
        holder_id: The agent ID of the caller (e.g.,
            ``"worker-TKT-NNN"``). Stored verbatim.
        ttl_minutes: How long the lease is valid. Clamped to
            [_MIN_TTL_MINUTES, _MAX_TTL_MINUTES] per
            ``CONVENTIONS.md §Auto-derived lease_ttl_minutes``.

    Returns:
        The new ``fencing_token`` (a positive integer, strictly
        monotonically increasing per the BIGSERIAL sequence).

    Raises:
        LeaseHeldError: The ticket is already held by a non-expired
            lease. The exception carries the current ``holder_id`` and
            ``lease_expires_at`` so the caller can log the conflict.
    """
    if ttl_minutes < _MIN_TTL_MINUTES:
        ttl_minutes = _MIN_TTL_MINUTES
    if ttl_minutes > _MAX_TTL_MINUTES:
        ttl_minutes = _MAX_TTL_MINUTES

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    with _connect() as conn:
        _set_tenant(conn, tenant_id)
        try:
            with conn.cursor() as cur:
                # Atomic INSERT. ON CONFLICT DO NOTHING means a second
                # concurrent caller on the same ticket gets 0 rows
                # back (no fencing_token to return). We then SELECT the
                # existing row + check expiry to decide between
                # "stale, reap and retry" and "live, raise
                # LeaseHeldError".
                cur.execute(
                    """
                    INSERT INTO orchestrator.lease
                        (ticket_id, tenant_id, holder_id, lease_expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ticket_id) DO NOTHING
                    RETURNING fencing_token
                    """,
                    (ticket_id, tenant_id, holder_id, expires_at),
                )
                row = cur.fetchone()
                if row is not None:
                    # Happy path: fresh insert, return the new token.
                    token = int(row[0])
                    conn.commit()
                    return token

                # Conflict — read the existing row to decide.
                cur.execute(
                    """
                    SELECT holder_id, lease_expires_at
                      FROM orchestrator.lease
                     WHERE ticket_id = %s
                    """,
                    (ticket_id,),
                )
                existing = cur.fetchone()
                conn.rollback()
                if existing is None:
                    # Vanishingly rare race: the row was deleted between
                    # the INSERT and the SELECT. Re-raise as LeaseHeld
                    # so the caller's retry loop catches it.
                    raise LeaseHeldError(
                        holder_id="<unknown>",
                        lease_expires_at=expires_at,
                    )
                cur_holder, cur_expires = existing[0], existing[1]
                if cur_expires < datetime.now(timezone.utc):
                    # Stale lease — caller can reap + retry. We do NOT
                    # auto-reap here (the reaper is a separate batch
                    # operation per the AC); we surface the conflict
                    # and let the caller invoke ``reap_stale`` or
                    # ``release`` if appropriate.
                    raise LeaseHeldError(
                        holder_id=cur_holder,
                        lease_expires_at=cur_expires,
                    )
                raise LeaseHeldError(
                    holder_id=cur_holder,
                    lease_expires_at=cur_expires,
                )
        except Exception:
            conn.rollback()
            raise


def heartbeat(
    ticket_id: str,
    tenant_id: str,
    holder_id: str,
    fencing_token: int,
    ttl_minutes: int,
) -> int:
    """Extend the lease + rotate the fencing token.

    The UPDATE is gated on the current ``fencing_token`` matching the
    supplied one. If another heartbeat / claim has since advanced the
    token (e.g., the caller paused and another holder reaped + re-
    claimed), the UPDATE matches zero rows and we raise
    :class:`StaleFencingTokenError`.

    Args:
        ticket_id: The ticket whose lease is being extended.
        tenant_id: The caller's tenant UUID (string form). Required so
            the ``app.current_tenant`` GUC can be set on the
            transaction BEFORE the UPDATE (RLS would otherwise hide
            the row from the ``orchestrator_app`` role — see AC #2).
        holder_id: The caller's agent ID. MUST match the original
            claim — defends against "holder A heartbeats on holder B's
            lease" mistakes.
        fencing_token: The current token (the one the caller received
            from the most recent ``claim`` or ``heartbeat``).
        ttl_minutes: The new TTL window.

    Returns:
        The new ``fencing_token`` (rotated by the BIGSERIAL).

    Raises:
        StaleFencingTokenError: The supplied token is no longer the
            current maximum.
        LeaseNotFoundError: The row no longer exists (reaper deleted
            it, or a release raced the heartbeat).
    """
    if ttl_minutes < _MIN_TTL_MINUTES:
        ttl_minutes = _MIN_TTL_MINUTES
    if ttl_minutes > _MAX_TTL_MINUTES:
        ttl_minutes = _MAX_TTL_MINUTES

    new_expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    with _connect() as conn:
        _set_tenant(conn, tenant_id)
        try:
            with conn.cursor() as cur:
                # The Kleppmann pattern: ``WHERE fencing_token =
                # $supplied`` is the monotonic guard. Postgres returns
                # zero rows when the token has been rotated, which is
                # the canonical "stale holder" rejection. We then SELECT
                # the current row to surface the precise error.
                cur.execute(
                    """
                    UPDATE orchestrator.lease
                       SET lease_expires_at = %s,
                           fencing_token    = nextval('orchestrator.lease_fencing_token_seq')
                     WHERE ticket_id     = %s
                       AND holder_id     = %s
                       AND fencing_token = %s
                    RETURNING fencing_token
                    """,
                    (new_expires_at, ticket_id, holder_id, fencing_token),
                )
                updated = cur.fetchone()
                if updated is not None:
                    token = int(updated[0])
                    conn.commit()
                    return token

                # Zero rows: the token is stale, OR the holder_id
                # doesn't match. SELECT the current row to disambiguate.
                cur.execute(
                    """
                    SELECT holder_id, fencing_token
                      FROM orchestrator.lease
                     WHERE ticket_id = %s
                    """,
                    (ticket_id,),
                )
                existing = cur.fetchone()
                conn.rollback()
                if existing is None:
                    raise LeaseNotFoundError(f"no lease found for ticket_id={ticket_id!r}")
                cur_holder, cur_token = existing[0], existing[1]
                if cur_holder != holder_id:
                    # The lease belongs to a different holder. Treat
                    # as a stale token error — the caller is operating
                    # on a lease they don't own.
                    raise StaleFencingTokenError(
                        supplied_token=fencing_token,
                        current_token=cur_token,
                    )
                raise StaleFencingTokenError(
                    supplied_token=fencing_token,
                    current_token=cur_token,
                )
        except Exception:
            conn.rollback()
            raise


def release(
    ticket_id: str,
    tenant_id: str,
    holder_id: str,
    fencing_token: int,
) -> None:
    """Delete the lease iff the supplied ``fencing_token`` is current.

    A stale release is rejected (so two holders can't both think they
    released the same lease). The Kleppmann guard: ``WHERE
    fencing_token = $supplied`` returns zero rows when the token has
    been rotated.

    Args:
        ticket_id: The ticket whose lease is being released.
        tenant_id: The caller's tenant UUID (string form). Required
            for the same reason as :func:`heartbeat` — the RLS policy
            hides the row from ``orchestrator_app`` when the GUC is unset.
        holder_id: The caller's agent ID. Must match the original
            claim (defends against "holder A releases holder B's
            lease" mistakes).
        fencing_token: The current token (the one returned by the most
            recent ``claim`` or ``heartbeat``).

    Raises:
        StaleFencingTokenError: The supplied token is no longer the
            current maximum (a heartbeat has since rotated the token,
            or another holder reaped + reclaimed).
        LeaseNotFoundError: The row no longer exists (the reaper
            already deleted it).
    """
    with _connect() as conn:
        _set_tenant(conn, tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM orchestrator.lease
                     WHERE ticket_id     = %s
                       AND holder_id     = %s
                       AND fencing_token = %s
                    RETURNING fencing_token
                    """,
                    (ticket_id, holder_id, fencing_token),
                )
                deleted = cur.fetchone()
                if deleted is not None:
                    conn.commit()
                    return None

                cur.execute(
                    """
                    SELECT holder_id, fencing_token
                      FROM orchestrator.lease
                     WHERE ticket_id = %s
                    """,
                    (ticket_id,),
                )
                existing = cur.fetchone()
                conn.rollback()
                if existing is None:
                    # Row vanished between SELECTs — reaper ran.
                    raise LeaseNotFoundError(f"no lease found for ticket_id={ticket_id!r}")
                cur_holder, cur_token = existing[0], existing[1]
                raise StaleFencingTokenError(
                    supplied_token=fencing_token,
                    current_token=cur_token,
                )
        except Exception:
            conn.rollback()
            raise


def read_lease(
    ticket_id: str,
    tenant_id: str,
) -> dict | None:
    """Read the current lease for ``ticket_id`` (read-only convenience).

    Returns the row as a dict with keys ``ticket_id``, ``tenant_id``,
    ``holder_id``, ``lease_expires_at`` (ISO 8601 string), ``fencing_token``
    (int), and ``claimed_at`` (ISO 8601 string), or ``None`` when no
    row is visible (no live lease for this ticket under this tenant).

    Sets ``app.current_tenant`` GUC before the query (per AC #2). The
    read-only contract makes this safe to use as the markdown-cache
    mirror described in AC #3 — the frontmatter's ``lease_*`` fields
    can be regenerated from ``read_lease()`` on every cascade update.
    """
    with _connect() as conn:
        _set_tenant(conn, tenant_id)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ticket_id,
                           tenant_id::text,
                           holder_id,
                           lease_expires_at,
                           fencing_token,
                           claimed_at
                      FROM orchestrator.lease
                     WHERE ticket_id = %s
                    """,
                    (ticket_id,),
                )
                row = cur.fetchone()
                conn.commit()
                if row is None:
                    return None
                return {
                    "ticket_id": row[0],
                    "tenant_id": row[1],
                    "holder_id": row[2],
                    "lease_expires_at": row[3].isoformat(),
                    "fencing_token": int(row[4]),
                    "claimed_at": row[5].isoformat(),
                }
        except Exception:
            conn.rollback()
            raise


def reap_stale(now: datetime | None = None) -> int:
    """Delete every lease whose ``lease_expires_at < now``.

    Safe to run on a cron; idempotent. Returns the count of rows
    deleted. The query is NOT tenant-scoped — the reaper is a system
    operation that runs as a privileged caller and cleans up every
    tenant's stale leases in one sweep. (The RLS policy still applies;
    if the connection is ``orchestrator_app``, the reaper sees only its own
    tenant's stale leases. For a global sweep, connect as ``orchestrator``
    — see ``tools/scripts/the project's init-app-role recipe.sh`` for the role hierarchy.)

    Args:
        now: The cutoff timestamp. Defaults to ``datetime.now(UTC)``.
            Exposed for deterministic testing.

    Returns:
        The number of rows deleted (an integer; 0 when nothing was
            stale).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    with _connect() as conn:
        # The reaper does not need a tenant context (it sweeps every
        # tenant). For the least-privileged ``orchestrator_app`` role, RLS
        # means we only see the caller's own tenant's stale leases —
        # the cron job is expected to run with the per-tenant
        # ``ASTRA_DB_TENANT`` env var set, OR to be invoked as
        # ``orchestrator`` for the platform-wide sweep. Without setting the
        # GUC, RLS returns zero rows (defense in depth — the reaper
        # will be a no-op rather than a cross-tenant data leak).
        if "ASTRA_DB_TENANT" in os.environ:
            _set_tenant(conn, os.environ["ASTRA_DB_TENANT"])
        else:
            # Explicit clear so we don't inherit a stale GUC from a
            # previous test in the same session. RLS will return zero
            # rows — that's correct for a non-tenant-scoped reaper
            # running as ``orchestrator_app``.
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_tenant', NULL, true)")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM orchestrator.lease WHERE lease_expires_at < %s",
                    (now,),
                )
                count = cur.rowcount
                conn.commit()
                return int(count)
        except Exception:
            conn.rollback()
            raise


__all__ = [
    "claim",
    "heartbeat",
    "release",
    "reap_stale",
    "read_lease",
    "LeaseError",
    "LeaseHeldError",
    "StaleFencingTokenError",
    "LeaseNotFoundError",
]
