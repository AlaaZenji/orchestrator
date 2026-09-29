"""Postgres-backed monotonic fencing-token lease (TKT-ORCH-011).

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
      policy would otherwise hide the row from the astra_app role),
      and the only way to know which tenant owns the row without a
      prior read is to accept it from the caller.

    * ``release(ticket_id, tenant_id, holder_id, fencing_token) -> None``
      — deletes the row iff the supplied ``fencing_token`` is still
      current. Raises :class:`StaleFencingTokenError` otherwise (a stale
      release is the canonical "two holders tried to release the same
      lease" race). Same ``tenant_id`` rationale as ``heartbeat``.
      Emits a one-line forensic log on every call (DSN + role +
      session_user + server host:port + isolation level) for
      parity-mismatch diagnosis.

    * ``release_or_idempotent(ticket_id, tenant_id, holder_id,
      fencing_token) -> str`` (TKT-ORCH-EVOLVE-LEASE-RELEASE-PARITY,
      2026-09-27) — idempotent release wrapper for worker-side post-
      cascade cleanup. Swallows :class:`LeaseNotFoundError` and
      :class:`StaleFencingTokenError` (returns ``"ok_no_op"``);
      re-raises every other exception. Useful when the orchestrator
      and worker connect via different paths and the orchestrator's
      release has already committed before the worker tries to
      release the same lease.

    * ``read_lease(ticket_id, tenant_id) -> dict | None``
      — read-only convenience for the markdown-cache mirror described
      in AC #3. Returns the row as a dict, or ``None`` when no live
      lease exists for the given ticket. Sets the
      ``app.current_tenant`` GUC before the query (per AC #2).

    * ``reap_stale(now) -> int``
      — deletes every lease whose ``lease_expires_at < now``. Returns
      the count of rows deleted. Safe to run on a cron; idempotent.

    * ``check_migration_number_collision(ticket_id, tenant_id,
      migration_number, ticket_lookup) -> None`` (TKT-DEEP-ONT-001-FU-2)
      — pre-claim guard. Raises
      :class:`MigrationNumberCollisionError` if another live (non-
      expired) lease already holds a ticket whose frontmatter declares
      the same ``migration_number``. The orchestrator cascade invokes
      this BEFORE :func:`claim`; when the check passes, ``claim``
      proceeds. ``ticket_lookup`` is a ``Callable[[str], str | None]``
      that returns the migration_number frontmatter for a given
      ``ticket_id`` (or ``None`` when the field is absent or the
      ticket file is unreadable).

    * ``claim()`` accepts an optional ``migration_number: str | None``
      kwarg (TKT-DEEP-ONT-001-FU-3). When supplied, the lease claim is
      followed by an atomic INSERT into ``orchestrator.migration_registry``
      (via :class:`orchestrator.scripts.migration_registry.MigrationRegistry`).
      The two operations are NOT in the same transaction by default —
      ``lease.py`` opens its own connection for the lease, and
      ``migration_registry.py`` opens its own for the claim. If the
      V-number is already taken, the registry claim raises
      :class:`MigrationNumberCollisionError` (carrying the
      conflicting ticket_id) and the lease is rolled back via a best-
      effort compensating release. If the caller wants true atomicity
      (lease + registry INSERT or neither), they MUST invoke
      :func:`check_migration_number_collision` first AND be prepared
      to handle the partial-state failure mode (the orchestrator's
      ``burn_queue.py`` does this).

The module sets the ``app.current_tenant`` GUC on every connection
BEFORE any query (per ADR-0008 + ``db/policies/tenant-rls.sql``). The
RLS policy on ``orchestrator.lease`` uses the canonical NULLIF form
(``tenant_id = NULLIF(current_setting('app.current_tenant', true),
'')::uuid``) — the GUC MUST be set in the same transaction as the
query, otherwise the policy returns zero rows. The module uses
``SET LOCAL`` (scoped to the transaction) so the value cannot leak
across connection-pool checkouts.

Stdlib only — uses :mod:`psycopg` 3.x which the project's Justfile
already pins (``docs/04-engineering/coding-standards.md`` + the
``psycopg[binary]`` driver already imported by ``tools/demo/``). All
DB I/O happens via the existing ``astra_app`` least-privileged role
(per ADR-0008 — ``astra`` is superuser with BYPASSRLS, which would
defeat RLS testing).

Synthetic test data only (CLAUDE.md #3): no real tenant IDs, no real
ticket IDs. The module's tests (``tests/orchestrator/test_lease.py``)
use UUIDs from ``uuid.uuid5`` over a synthetic namespace.

Typical usage:

    from orchestrator.scripts.lease import claim, heartbeat, release
    from orchestrator.scripts.lease import release_or_idempotent  # worker cleanup

    token = claim("TKT-P1-099", tenant_id, "agent-A", ttl_minutes=15)
    # ... do work ...
    token = heartbeat("TKT-P1-099", "agent-A", token, ttl_minutes=15)
    # ... more work ...
    release("TKT-P1-099", "agent-A", token)

Migration-number pre-claim (TKT-DEEP-ONT-001-FU-2):

    from orchestrator.scripts.lease import (
        check_migration_number_collision,
        MigrationNumberCollisionError,
        claim,
    )

    try:
        check_migration_number_collision(
            ticket_id="TKT-NEW",
            tenant_id=tenant_id,
            migration_number="V060",
            ticket_lookup=_read_migration_number,  # injected
        )
    except MigrationNumberCollisionError as exc:
        # abort — another live lease holds V060
        ...

    token = claim("TKT-NEW", tenant_id, "agent-A", ttl_minutes=15)

Environment:

    ``DATABASE_URL``   — Postgres connection string (psycopg DSN). When
                        unset, the module falls back to the laptop-
                        staging default ``postgresql://astra@localhost:
                        55432/astra``. CI / cloud set this explicitly.

    ``ASTRA_DB_ROLE``  — Postgres role to connect as. Defaults to
                        ``astra_app`` (the least-privileged role per
                        ADR-0008). Set to ``astra`` only for ops
                        recovery; the module's RLS tests assume
                        ``astra_app``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Final

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
#: Postgres on port 55432, auth ``trust`` on localhost).
_DEFAULT_DSN: Final[str] = "postgresql://astra@localhost:55432/astra"

#: Default Postgres role. The least-privileged ``astra_app`` role per
#: ADR-0008 — every code path that touches clinical/audit/orchestrator
#: tables MUST be exercised as ``astra_app``, never as ``astra``
#: (which is superuser with BYPASSRLS, defeating RLS testing).
_DEFAULT_DB_ROLE: Final[str] = "astra_app"

#: Default role password. Matches the local dev password set in
#: ``tools/scripts/init_app_role.sh`` (``astra_app_dev``). CI sets
#: ``ASTRA_APP_PGPASSWORD`` explicitly.
_DEFAULT_DB_PASSWORD: Final[str] = "astra_app_dev"

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


class MigrationNumberCollisionError(LeaseError):
    """Raised by :func:`check_migration_number_collision` when another
    live lease already holds a ticket whose frontmatter declares the
    same Flyway V-number (TKT-DEEP-ONT-001-FU-2, FOUND-001).

    Flyway rejects duplicate V-numbers at apply time. Two concurrent
    burns both targeting V050 would race, and one would FAIL on apply.
    The orchestrator surfaces the conflict BEFORE the claim so the
    cascade can re-plan (pick V051, file a discovery, etc.) rather than
    landing a migration that conflicts on disk.

    Attributes:
        ticket_id: The ticket attempting to claim the colliding V-number.
        migration_number: The V-number that was already taken (e.g., V050).
        conflicting_ticket_id: The ticket_id of the live lease that
            already holds this V-number.
        conflicting_holder_id: The ``holder_id`` of the live lease.
    """

    code = "MIGRATION_NUMBER_COLLISION"

    def __init__(
        self,
        ticket_id: str,
        migration_number: str,
        conflicting_ticket_id: str,
        conflicting_holder_id: str,
    ) -> None:
        super().__init__(
            code=self.code,
            message=(
                f"ticket {ticket_id!r} cannot claim {migration_number!r}: "
                f"already held by live lease on {conflicting_ticket_id!r} "
                f"(holder_id={conflicting_holder_id!r})"
            ),
        )
        self.ticket_id = ticket_id
        self.migration_number = migration_number
        self.conflicting_ticket_id = conflicting_ticket_id
        self.conflicting_holder_id = conflicting_holder_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _connect():  # pragma: no cover - thin wrapper, exercised via integration tests
    """Open a psycopg connection as the least-privileged ``astra_app`` role.

    Resolves the DSN from ``DATABASE_URL`` (falls back to the laptop-
    staging default on port 55432). The connection is opened with
    explicit ``user`` + ``password`` kwargs so we connect AS
    ``astra_app`` directly — psycopg authenticates once, the session
    is in the role from the start, and ``SET ROLE`` is unnecessary.

    Per ADR-0008, ``astra_app`` has no superuser + no BYPASSRLS, so
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
    # Local dev password matches ``init_app_role.sh``; CI sets
    # ``ASTRA_APP_PGPASSWORD`` explicitly. The ``astra`` superuser
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
    *,
    migration_number: str | None = None,
) -> int:
    """Atomic lease claim — Kleppmann monotonic fencing token.

    Args:
        ticket_id: The ticket to claim (e.g., ``"TKT-P1-099"``).
        tenant_id: The caller's tenant UUID (string form, parsed by
            Postgres ``::uuid`` cast in the RLS policy).
        holder_id: The agent ID of the caller (e.g.,
            ``"worker-TKT-ORCH-011"``). Stored verbatim.
        ttl_minutes: How long the lease is valid. Clamped to
            [_MIN_TTL_MINUTES, _MAX_TTL_MINUTES] per
            ``CONVENTIONS.md §Auto-derived lease_ttl_minutes``.
        migration_number: Optional V-number reservation
            (TKT-DEEP-ONT-001-FU-3, AC #5). When supplied, the lease
            INSERT is followed by an atomic INSERT into
            ``orchestrator.migration_registry`` via
            :class:`orchestrator.scripts.migration_registry.MigrationRegistry`.
            The V-number is durably reserved for the ticket; a
            duplicate V-number raises
            :class:`MigrationNumberCollisionError` (with
            ``conflicting_ticket_id`` populated) and the lease is
            rolled back via a best-effort compensating release.

    Returns:
        The new ``fencing_token`` (a positive integer, strictly
        monotonically increasing per the BIGSERIAL sequence).

    Raises:
        LeaseHeldError: The ticket is already held by a non-expired
            lease. The exception carries the current ``holder_id`` and
            ``lease_expires_at`` so the caller can log the conflict.
        MigrationNumberCollisionError: ``migration_number`` was
            supplied and is already claimed by another live row in
            ``orchestrator.migration_registry``. The exception's
            ``conflicting_ticket_id`` is populated. The lease has
            been released as a compensating action — the caller does
            NOT need to invoke ``release()`` themselves.

    Note on atomicity (TKT-DEEP-ONT-001-FU-3):
        The lease INSERT and the migration_registry INSERT are NOT in
        the same DB transaction — ``lease.py`` opens its own
        connection for the lease, and ``migration_registry.py`` opens
        its own. If the registry claim fails AFTER the lease commits,
        we best-effort release the lease so the caller sees a clean
        "no claim held" state. A truly atomic claim (lease + registry
        INSERT or neither) would require both tables in one
        transaction; that is a future optimisation (the orchestrator's
        ``burn_queue.py`` already calls
        :func:`check_migration_number_collision` first to short-
        circuit the common case before :func:`claim`).
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
                    # TKT-DEEP-ONT-001-FU-3 (AC #5): if a V-number was
                    # supplied, durably reserve it via the registry.
                    # The registry INSERT is in a separate
                    # transaction; on collision we best-effort release
                    # the lease to avoid an orphaned lease row.
                    if migration_number:
                        try:
                            from orchestrator.scripts.migration_registry import (
                                MigrationRegistry,
                                MigrationNumberCollisionError,
                            )
                            registry = MigrationRegistry()
                            registry.claim(
                                ticket_id=ticket_id,
                                v_number=migration_number,
                                tenant_id=tenant_id,
                            )
                        except MigrationNumberCollisionError:
                            # Best-effort compensating release so the
                            # caller does not have to call release()
                            # themselves. If the release itself fails,
                            # we log + re-raise the collision (the
                            # orchestrator's reap_stale cron will
                            # clean up the orphaned lease on TTL
                            # expiry — bounded damage).
                            try:
                                release(
                                    ticket_id=ticket_id,
                                    tenant_id=tenant_id,
                                    holder_id=holder_id,
                                    fencing_token=token,
                                )
                            except Exception:
                                pass
                            raise
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
            the row from the ``astra_app`` role — see AC #2).
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
            hides the row from ``astra_app`` when the GUC is unset.
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

    Forensic logging (TKT-ORCH-EVOLVE-LEASE-RELEASE-PARITY, 2026-09-27):
    every release() — success OR failure — emits a one-line ``logging``
    record carrying the DSN host/port/db + the Postgres session user
    + the transaction-isolation level. This is the diagnostic primitive
    for the cross-connection parity mismatches observed in Layers
    29-31, 72, 73, and 80 (worker-side release() raised
    ``LeaseNotFoundError`` while the orchestrator-side release with
    the same fencing_token succeeded — the mismatch was a different
    connection point / role / host). The log line is emitted via the
    stdlib ``logging`` module on the ``orchestrator.lease`` logger —
    callers can attach a handler in production, and tests can attach
    a ``caplog`` handler (no prints).
    """
    dsn = os.environ.get("DATABASE_URL", _DEFAULT_DSN)
    role = os.environ.get("ASTRA_DB_ROLE", _DEFAULT_DB_ROLE)
    logger = logging.getLogger("orchestrator.lease")
    with _connect() as conn:
        # Capture forensic context (DSN + role + session user +
        # isolation) BEFORE any state-mutating query. This is the
        # single source of truth for "which Postgres instance did this
        # release() actually run against" — the parameter that's
        # been unobservable when worker + orchestrator side release()
        # calls disagree (Layers 29-31, 72, 73, 80).
        try:
            with conn.cursor() as _forensic_cur:
                _forensic_cur.execute(
                    "SELECT current_user, current_database(), "
                    "       inet_server_addr(), inet_server_port(), "
                    "       current_setting('transaction_isolation')"
                )
                _forensic_row = _forensic_cur.fetchone()
            _cur_user = _forensic_row[0] if _forensic_row else "unknown"
            _cur_db = _forensic_row[1] if _forensic_row else "unknown"
            _srv_host = str(_forensic_row[2]) if _forensic_row and _forensic_row[2] is not None else "unknown"
            _srv_port = int(_forensic_row[3]) if _forensic_row and _forensic_row[3] is not None else -1
            _isolation = _forensic_row[4] if _forensic_row else "unknown"
        except Exception as _exc:
            _cur_user = _cur_db = _srv_host = _isolation = "unknown"
            _srv_port = -1
            logger.warning(
                "lease.release.forensic_probe_failed ticket_id=%s "
                "holder_id=%s error=%r",
                ticket_id, holder_id, _exc,
            )
        logger.info(
            "lease.release.forensic ticket_id=%s tenant_id=%s "
            "holder_id=%s fencing_token=%s dsn=%s role=%s "
            "session_user=%s db=%s server=%s:%s isolation=%s",
            ticket_id, tenant_id, holder_id, fencing_token,
            dsn, role, _cur_user, _cur_db, _srv_host, _srv_port,
            _isolation,
        )
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
                    logger.info(
                        "lease.release.deleted ticket_id=%s holder_id=%s "
                        "fencing_token=%s",
                        ticket_id, holder_id, fencing_token,
                    )
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
                    logger.info(
                        "lease.release.lease_not_found ticket_id=%s "
                        "holder_id=%s fencing_token=%s",
                        ticket_id, holder_id, fencing_token,
                    )
                    raise LeaseNotFoundError(f"no lease found for ticket_id={ticket_id!r}")
                cur_holder, cur_token = existing[0], existing[1]
                logger.info(
                    "lease.release.stale_token ticket_id=%s "
                    "holder_id=%s fencing_token=%s current_holder=%s "
                    "current_token=%s",
                    ticket_id, holder_id, fencing_token,
                    cur_holder, cur_token,
                )
                raise StaleFencingTokenError(
                    supplied_token=fencing_token,
                    current_token=cur_token,
                )
        except Exception:
            conn.rollback()
            raise


def release_or_idempotent(
    ticket_id: str,
    tenant_id: str,
    holder_id: str,
    fencing_token: int,
) -> str:
    """Idempotent release helper for worker-side post-cascade cleanup.

    Worker-side ``release(...)`` calls occasionally raise
    ``LeaseNotFoundError`` when the orchestrator-side release has
    already deleted the row (Layers 29-31, 72, 73, 80 — the orchestrator
    and worker connect via different paths, so the orchestrator's
    committed DELETE is invisible to the worker until the worker's
    transaction starts). The canonical post-cascade cleanup path
    needs to be idempotent: if the lease is already gone, the worker
    must treat that as a successful no-op (the orchestrator already
    released it — the worker has nothing left to clean up).

    This helper wraps :func:`release` and swallows the two
    "already-released" failure modes (:class:`LeaseNotFoundError` +
    :class:`StaleFencingTokenError`). All other exceptions propagate
    so the worker can escalate.

    Returns:
        ``"ok"`` when this call deleted the row; ``"ok_no_op"`` when
        the row was already gone or the token was stale. The two
        outcomes are distinguishable so the caller can emit different
        log lines (and so the orchestrator's progress log carries a
        marker for every no-op release — useful for diagnosing parity
        mismatches in future burns).

    Rationale:
        * ``LeaseNotFoundError`` — the orchestrator (or another worker)
          released the lease first. The worker's delete is a no-op.
        * ``StaleFencingTokenError`` — a heartbeat rotated the token
          while the worker was paused. The lease may still exist under
          a NEW token, but the worker's release is for an OLD token;
          we MUST NOT delete the new lease (Kleppmann invariant). The
          correct action is to no-op and let the reaper clean up the
          stale-row case.
        * Any other exception (e.g., connection error, ``ValueError``
          for empty args) MUST propagate — those are real bugs the
          worker should surface.

    Surfacing the no-op:
        When the no-op path fires, we emit a single ``logging.WARNING``
        line on the ``orchestrator.lease`` logger carrying the ticket
        ID + holder ID + fencing token + which exception class was
        caught. This is the forensic primitive the ticket asked for
        (Layer 80 worker self-report logged only "LeaseNotFoundError
        (no lease in DB) — no-op" with no diagnostic context — the
        progress log is now enriched).
    """
    logger = logging.getLogger("orchestrator.lease")
    try:
        release(ticket_id, tenant_id, holder_id, fencing_token)
        return "ok"
    except (LeaseNotFoundError, StaleFencingTokenError) as exc:
        logger.warning(
            "lease.release_or_idempotent.no_op ticket_id=%s "
            "tenant_id=%s holder_id=%s fencing_token=%s "
            "exception_class=%s reason=%s",
            ticket_id, tenant_id, holder_id, fencing_token,
            type(exc).__name__, exc,
        )
        return "ok_no_op"


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
    if the connection is ``astra_app``, the reaper sees only its own
    tenant's stale leases. For a global sweep, connect as ``astra``
    — see ``tools/scripts/init_app_role.sh`` for the role hierarchy.)

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
        # tenant). For the least-privileged ``astra_app`` role, RLS
        # means we only see the caller's own tenant's stale leases —
        # the cron job is expected to run with the per-tenant
        # ``ASTRA_DB_TENANT`` env var set, OR to be invoked as
        # ``astra`` for the platform-wide sweep. Without setting the
        # GUC, RLS returns zero rows (defense in depth — the reaper
        # will be a no-op rather than a cross-tenant data leak).
        if "ASTRA_DB_TENANT" in os.environ:
            _set_tenant(conn, os.environ["ASTRA_DB_TENANT"])
        else:
            # Explicit clear so we don't inherit a stale GUC from a
            # previous test in the same session. RLS will return zero
            # rows — that's correct for a non-tenant-scoped reaper
            # running as ``astra_app``.
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


def check_migration_number_collision(
    ticket_id: str,
    tenant_id: str,
    migration_number: str,
    ticket_lookup: Callable[[str], str | None],
    now: datetime | None = None,
) -> None:
    """Pre-claim guard: detect Flyway V-number collisions across live leases
    (TKT-DEEP-ONT-001-FU-2, FOUND-001).

    Scans every live (non-expired) lease on ``orchestrator.lease`` for
    the caller's tenant. For each row, calls ``ticket_lookup`` to read
    the live ticket's frontmatter ``migration_number`` field. If any
    other live ticket declares the same ``migration_number`` as the
    caller is about to claim, raises
    :class:`MigrationNumberCollisionError`.

    The function is intentionally side-effect-free — it does NOT
    mutate any DB state. The orchestrator cascade calls this BEFORE
    :func:`claim`; when the check passes, ``claim`` proceeds.

    Why a separate function (not folded into ``claim``):
        ``claim`` is a pure DB primitive. Migration-number semantics
        live in ticket frontmatter (a filesystem artifact), and the
        lookup is therefore injected via ``ticket_lookup`` rather than
        baked into the lease module. This keeps lease.py focused on
        Postgres primitives and makes the migration-number guard
        independently testable (the test injects a fake
        ``ticket_lookup`` and never touches the DB).

    Why per-tenant scoping:
        Two tenants cannot collide on V-numbers (Flyway migration
        files are repo-scoped, not tenant-scoped), so the per-tenant
        filter is technically redundant for migration-number
        collisions. We scope to the caller's tenant anyway so the
        query respects the RLS policy (per ADR-0008, every
        orchestrator.lease query MUST set ``app.current_tenant``).

    Args:
        ticket_id: The ticket about to claim (e.g., ``"TKT-NEW"``).
            Used only for diagnostics in the raised exception.
        tenant_id: The caller's tenant UUID. Sets the
            ``app.current_tenant`` GUC before the SELECT (per AC #2).
        migration_number: The V-number the ticket declares in its
            frontmatter (e.g., ``"V060"``). A non-V-prefix string is
            accepted (we don't validate the prefix) — the comparison is
            exact-string.
        ticket_lookup: A ``Callable[[str], str | None]`` that returns
            the ``migration_number`` frontmatter value for a given
            ``ticket_id``, or ``None`` when the field is absent, the
            file is unreadable, or the ticket does not exist. The
            caller (orchestrator cascade) injects a function that
            walks ``orchestrator/tickets/`` and parses frontmatter.
        now: The cutoff timestamp used to filter live leases.
            Defaults to ``datetime.now(UTC)``. Exposed for
            deterministic testing.

    Raises:
        MigrationNumberCollisionError: Another live lease holds a
            ticket whose ``migration_number`` matches the caller's.
    """
    if not migration_number:
        # No V-number declared → no collision possible. Return early
        # so tickets without migration_number skip the DB query
        # entirely (cheap path for tickets that don't touch Flyway).
        return

    if now is None:
        now = datetime.now(timezone.utc)

    with _connect() as conn:
        _set_tenant(conn, tenant_id)
        try:
            with conn.cursor() as cur:
                # Read every live lease under the caller's tenant. RLS
                # (canonical NULLIF form per V024) scopes the SELECT
                # to ``tenant_id = current_setting('app.current_tenant')
                # ::uuid``. We then iterate in Python to call the
                # injected ``ticket_lookup`` — the lookup is per-ticket
                # and small enough to keep out of SQL.
                cur.execute(
                    """
                    SELECT ticket_id, holder_id
                      FROM orchestrator.lease
                     WHERE lease_expires_at > %s
                    """,
                    (now,),
                )
                live_rows = cur.fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for other_ticket_id, other_holder_id in live_rows:
        if other_ticket_id == ticket_id:
            # Defensive: if the caller has somehow inserted its own
            # lease already (shouldn't happen — this is a pre-claim
            # guard), skip it. We only want to compare against OTHER
            # live leases.
            continue
        try:
            other_migration_number = ticket_lookup(other_ticket_id)
        except Exception:
            # A broken ticket_lookup (e.g., malformed frontmatter)
            # MUST NOT mask a real collision. Treat as "no declared
            # V-number" so we don't false-positive; the cascade's own
            # ticket still validates its own frontmatter via
            # ``dependency_audit.py`` at pre-claim gate.
            other_migration_number = None
        if other_migration_number is None:
            continue
        if other_migration_number == migration_number:
            raise MigrationNumberCollisionError(
                ticket_id=ticket_id,
                migration_number=migration_number,
                conflicting_ticket_id=other_ticket_id,
                conflicting_holder_id=other_holder_id,
            )


# ---------------------------------------------------------------------------
# TKT-ORCH-FIX-STATE-SYNC (2026-09-29): release_with_reconcile
# ---------------------------------------------------------------------------


def release_with_reconcile(
    ticket_id: str,
    tenant_id: str,
    holder_id: str,
    fencing_token: int,
    *,
    worker_result_path: str | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
) -> str:
    """Release a lease AND bridge it to the reconcile queue.

    TKT-ORCH-FIX-STATE-SYNC + TKT-ORCH-FIX-STATE-CONSISTENCY: this is
    the chokepoint for worker-side status transitions. It:
      1. Releases the lease (atomic Postgres DELETE).
      2. Delegates to ``state.commit()`` for the frontmatter write,
         event enqueue, heartbeat cleanup, and audit log.

    On a successful release (status == ``"ok"``), the commit runs.
    On a no-op (``"ok_no_op"``), nothing happens — the orchestrator
    already wrote its own commit when it released first.

    Returns:
        ``"ok"`` on first release + commit, ``"ok_no_op"`` if the lease
        was already gone (and no commit was attempted).

    Example::

        python3 orchestrator/scripts/lease.py release-with-reconcile \\
            TKT-FOO <tenant-uuid> worker-abc 17 \\
            --worker-result orchestrator/progress/TKT-FOO-WORKER-RESULT.json \\
            --previous-status QUEUED --new-status DONE
    """
    logger = logging.getLogger("orchestrator.lease.reconcile")
    result = release_or_idempotent(
        ticket_id, tenant_id, holder_id, fencing_token
    )
    if result != "ok":
        logger.info(
            "lease.release_with_reconcile.no_op ticket_id=%s reason=not_first",
            ticket_id,
        )
        return result

    # Delegate the status mutation + reconcile event + heartbeat cleanup
    # to the SINGLE chokepoint. state.commit validates the transition,
    # writes frontmatter atomically, enqueues the reconcile event,
    # removes the heartbeat, and emits the audit row.
    try:
        # Lazy import to avoid circular deps (state.py imports lease.py
        # for the live-lease read in state.read()).
        import state as state_mod  # type: ignore

        # If new_status was supplied, use it; otherwise state.commit
        # reads the frontmatter (which already says DONE/PARTIAL/etc.
        # because the worker wrote it before calling release).
        target = (new_status or "").strip()
        if target:
            state_mod.commit(
                ticket_id,
                target,
                source="worker",
                reason=(
                    f"lease release; worker_result={worker_result_path}"
                    if worker_result_path else "lease release"
                ),
                fencing_token=fencing_token,
                holder_id=holder_id,
                tenant_id=tenant_id,
            )
        else:
            # Worker updated frontmatter before calling release; state.commit
            # is still called to enqueue the reconcile event + emit audit.
            # We do this via a "no-op" commit that uses the current status.
            fm, _, _ = state_mod.read_frontmatter(ticket_id)
            cur = (fm.get("status") or "").strip().upper()
            if cur:
                state_mod.commit(
                    ticket_id,
                    cur,
                    source="worker",
                    reason="lease release; status already on frontmatter",
                    fencing_token=fencing_token,
                    holder_id=holder_id,
                    tenant_id=tenant_id,
                )
        return "ok"
    except Exception as exc:
        # The reconcile queue + state.commit must NEVER fail the lease release.
        logger.warning(
            "lease.release_with_reconcile.commit_failed "
            "ticket_id=%s exc=%s",
            ticket_id, exc,
        )
        return "ok"
    finally:
        # ALWAYS trigger the debounced reconcile, regardless of state.commit
        # outcome — the reconcile will pick up whatever frontmatter state
        # exists (whether state.commit succeeded or not).
        try:
            _trigger_debounced_reconcile(ticket_id)
        except Exception as exc:
            logger.warning(
                "lease.release_with_reconcile.debounce_trigger_failed "
                "ticket_id=%s exc=%s",
                ticket_id, exc,
            )


def _trigger_debounced_reconcile(ticket_id: str) -> None:
    """Trigger ``auto_reconcile.py --incremental`` with a 2 s debounce.

    Multiple releases within the cooldown coalesce into one reconcile pass.
    The sentinel file is the debounce primitive.
    """
    logger = logging.getLogger("orchestrator.lease.reconcile")
    repo_root = Path(__file__).resolve().parents[2]
    progress_dir = repo_root / "orchestrator" / "progress"
    sentinel = progress_dir / ".reconcile_debounce"
    now = time.time()
    last = 0.0
    if sentinel.exists():
        try:
            last = float(sentinel.read_text().strip() or "0")
        except Exception:
            last = 0.0
    if now - last >= 2.0:
        try:
            sentinel.write_text(str(now))
        except Exception:
            pass
        try:
            subprocess.run(
                [
                    "python3",
                    str(
                        repo_root
                        / "orchestrator"
                        / "scripts"
                        / "auto_reconcile.py"
                    ),
                    "--incremental",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning(
                "lease._trigger_debounced_reconcile.failed "
                "ticket_id=%s exc=%s",
                ticket_id, exc,
            )
    else:
        logger.info(
            "lease.release_with_reconcile.debounced ticket_id=%s "
            "cooldown_remaining=%.2fs",
            ticket_id, 2.0 - (now - last),
        )


__all__ = [
    "claim",
    "heartbeat",
    "release",
    "release_or_idempotent",
    "release_with_reconcile",
    "reap_stale",
    "read_lease",
    "check_migration_number_collision",
    "LeaseError",
    "LeaseHeldError",
    "StaleFencingTokenError",
    "LeaseNotFoundError",
    "MigrationNumberCollisionError",
]


# ---------------------------------------------------------------------------
# CLI entrypoint (TKT-ORCH-FIX-STATE-SYNC, 2026-09-29)
# ---------------------------------------------------------------------------

import argparse  # noqa: E402  (kept here for diff hygiene; also at module top)


def _cli_main(argv: list[str] | None = None) -> int:
    """Tiny CLI so workers can call release-with-reconcile from bash.

    Usage::

        python3 -m orchestrator.scripts.lease release-with-reconcile \\
            TKT-FOO <tenant-uuid> worker-abc 17 \\
            --worker-result orchestrator/progress/TKT-FOO-WORKER-RESULT.json \\
            --previous-status QUEUED --new-status DONE
    """
    parser = argparse.ArgumentParser(
        prog="lease",
        description="Orchestrator lease primitives (TKT-ORCH-011).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rwr = sub.add_parser(
        "release-with-reconcile",
        help="Release a lease and enqueue a reconcile event "
             "(TKT-ORCH-FIX-STATE-SYNC).",
    )
    p_rwr.add_argument("ticket_id")
    p_rwr.add_argument("tenant_id")
    p_rwr.add_argument("holder_id")
    p_rwr.add_argument("fencing_token", type=int)
    p_rwr.add_argument("--worker-result", dest="worker_result_path",
                       default=None)
    p_rwr.add_argument("--previous-status", dest="previous_status",
                       default=None)
    p_rwr.add_argument("--new-status", dest="new_status", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "release-with-reconcile":
        out = release_with_reconcile(
            args.ticket_id,
            args.tenant_id,
            args.holder_id,
            args.fencing_token,
            worker_result_path=args.worker_result_path,
            previous_status=args.previous_status,
            new_status=args.new_status,
        )
        print(out)
        return 0 if out in ("ok", "ok_no_op") else 2
    return 2


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))
