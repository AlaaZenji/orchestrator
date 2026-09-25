"""Transactional outbox writer for the orchestrator Step 6 cascade.

Per TKT-NNN (2026-09-19): every cascade sub-action (ticket frontmatter
update + tickets-index.md update + progress log + STATE.md update + commit)
is enqueued to ``orchestrator.outbox`` in a single Postgres transaction,
then applied by ``outbox_consumer.py`` in strict outbox_id order with
idempotency.

This module is the WRITE side only. It does not commit cascade updates
itself; it only guarantees that the row is durably enqueued so the
consumer can replay it. The complement is
``orchestrator/scripts/outbox_consumer.py``.

Industry-standard pattern: Chris Richardson's microservices.io
"Transactional Outbox" pattern. Microsoft "Transactional Outbox Pattern
with Cosmos DB" — same shape.

Idempotency
-----------
The ``idempotency_key UNIQUE`` constraint on the table is the dedup
primitive. A duplicate enqueue returns the EXISTING row's ``outbox_id`` —
``enqueue()`` raises :class:`DuplicateEnqueueError` so the caller can
distinguish a fresh insert from a replay without ambiguity, and the
consumer's ``mark_applied`` is naturally idempotent (a row whose
``applied_at`` is already set is skipped — see
``outbox_consumer.apply_row``).

Multi-tenancy
-------------
Per the project's decision records every connection MUST set ``app.current_tenant`` before
querying per-tenant tables. ``orchestrator.outbox`` has a canonical NULLIF
RLS policy (per ``db/migrations/V025__orchestrator_outbox.sql``), so a
connection without the GUC returns zero rows — the canonical defense in
depth. The connection helper :func:`_connect` opens the connection as the
least-privileged ``orchestrator_app`` role and sets the GUC up-front.

Stdlib-only where possible: psycopg is the single external dep, used
because no in-process Postgres alternative is available on the laptop
staging path. Stdlib-only for everything else (pathlib, dataclasses,
contextlib, json).
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import UUID

import psycopg
import psycopg.errors


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Repo root inferred from the script location: ``orchestrator/scripts/`` →
#: repo root. Matches the same convention used by every other script under
#: ``orchestrator/scripts/`` (checkpoint.py, regenerate_metrics.py, etc.).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ORCH_ROOT: Path = REPO_ROOT / "orchestrator"

#: Default DSN — matches the Justfile's DATABASE_URL convention. Override
#: with the ``DATABASE_URL`` env var (CI / laptop-staging-bootstrap path).
DEFAULT_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://orchestrator:orchestrator@localhost:${ORCHESTRATOR_DB_PORT:-5432}/orchestrator",
)

#: Role used for tenant-scoped queries. ``orchestrator`` (superuser, BYPASSRLS)
#: defeats RLS testing, so every code path that touches per-tenant tables
#: must exercise as ``orchestrator_app`` (TKT-NNN, 2026-09-18).
DEFAULT_APP_ROLE_DSN: str = os.environ.get(
    "ORCH_OUTBOX_DSN",
    "postgresql://orchestrator_app:orchestrator_app_dev@localhost:${ORCHESTRATOR_DB_PORT:-5432}/orchestrator",
)

#: Schema-qualified table name. Single source of truth for the migration
#: filename + this module's SQL — change BOTH if the table is renamed.
TABLE_NAME: str = "orchestrator.outbox"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutboxError(Exception):
    """Base class for all outbox-side errors."""


class DuplicateEnqueueError(OutboxError):
    """Raised by :func:`enqueue` when ``idempotency_key`` already exists.

    The UNIQUE constraint on ``idempotency_key`` is the canonical dedup
    primitive — duplicates are not silently swallowed. The caller may
    choose to fetch the existing row and reuse its ``outbox_id`` if it
    wants at-most-once apply semantics; the caller can also let the
    :class:`DuplicateEnqueueError` propagate so a higher layer surfaces
    the replay-attempt to the operator.
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            f"idempotency_key {idempotency_key!r} already enqueued; "
            f"the consumer must have applied it already or another writer "
            f"raced us. Caller MUST decide: reuse the existing row, or "
            f"abort."
        )
        self.idempotency_key = idempotency_key


class TenantContextRequired(OutboxError):
    """Raised when ``app.current_tenant`` is not set before a query.

    Per the project's decision records every per-tenant query MUST set the GUC up-front. The
    connection helpers in this module set it for you; calling the public
    API without a tenant context is a usage error.
    """

    pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxRow:
    """One row from ``orchestrator.outbox``.

    All fields are coerced to JSON-friendly primitives at read time so the
    dataclass can be safely serialised (json.dumps, log records, etc.)
    without further conversion.

    Attributes:
        outbox_id:        BIGSERIAL PK. Strict monotonic insertion order.
        tenant_id:        UUID of the tenant that owns the action.
        action:           The JSONB action payload (e.g. ``{"type": "edit_file",
                          "path": "...", "content": "..."}``).
        idempotency_key:  Caller-supplied dedup token (UNIQUE).
        created_at:       Postgres ``DEFAULT now()`` at insertion.
        applied_at:       ``None`` until the consumer marks it applied.
        applied_by:       ``None`` until the consumer marks it applied.
    """

    outbox_id: int
    tenant_id: UUID
    action: dict[str, Any]
    idempotency_key: str
    created_at: Any  # datetime, but kept loose to avoid datetime import cycle
    applied_at: Any | None
    applied_by: str | None

    def is_applied(self) -> bool:
        """True iff this row has been marked applied by a consumer."""
        return self.applied_at is not None


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


@contextmanager
def _connect(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a single-shot psycopg connection.

    The DSN defaults to :data:`DEFAULT_APP_ROLE_DSN` (the least-privileged
    ``orchestrator_app`` role). Callers MAY pass a different DSN (the admin DSN
    is used for bootstrap + the aggregate metrics queries in
    ``regenerate_metrics.py``). The connection is committed on clean exit
    and rolled back on exception; the connection is always closed.

    No GUC is set here — the caller decides whether the connection needs
    a tenant context (the public API sets it; admin queries don't).
    """
    chosen = dsn or DEFAULT_APP_ROLE_DSN
    conn = psycopg.connect(chosen)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def tenant_connection(
    tenant_id: UUID | str,
    *,
    dsn: str | None = None,
) -> Iterator[psycopg.Connection]:
    """Open a connection with ``app.current_tenant`` set to ``tenant_id``.

    Per the project's decision records every per-tenant query MUST run on a connection with the
    GUC set, or the canonical NULLIF RLS policy silently returns zero rows
    instead of the caller's intended rows (defense in depth).

    Args:
        tenant_id: The tenant UUID (or its string form) to scope to.
        dsn:      Optional DSN override (defaults to
                  :data:`DEFAULT_APP_ROLE_DSN`).

    Raises:
        TenantContextRequired: if the supplied ``tenant_id`` is empty.
    """
    tid = str(tenant_id).strip()
    if not tid:
        raise TenantContextRequired(
            "tenant_id is required (the project's decision records); refusing to open a "
            "tenant-scoped connection without one"
        )
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            # Postgres' `SET` command does NOT accept parameterised
            # values via the binary protocol — `cur.execute("SET
            # app.current_tenant = %s", ...)` raises
            # `psycopg.errors.SyntaxError: syntax error at or near "$1"`.
            # `set_config(setting_name, new_value, is_local)` is the
            # canonical function form that DOES accept parameters.
            # is_local=True mirrors the `SET LOCAL` semantics — the GUC
            # is reset when the transaction ends, which is the right
            # scope for a per-tenant connection.
            cur.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                (tid,),
            )
        yield conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(
    action: dict[str, Any],
    idempotency_key: str,
    tenant_id: UUID | str,
    *,
    dsn: str | None = None,
) -> int:
    """Insert one outbox row.

    The ``idempotency_key UNIQUE`` constraint catches duplicate enqueue
    attempts: the second call with the same key raises
    :class:`DuplicateEnqueueError` so the caller can distinguish a fresh
    insert from a replay.

    Args:
        action:          The JSONB action payload. Must be a dict; nested
                         dicts + lists + scalars are serialised by the
                         psycopg JSONB adapter.
        idempotency_key: A non-empty dedup token. The same token twice
                         raises :class:`DuplicateEnqueueError`.
        tenant_id:       The tenant UUID (or string form) that owns the
                         action. Sets ``app.current_tenant`` for RLS.
        dsn:             Optional DSN override (default: least-privileged
                         ``orchestrator_app`` role).

    Returns:
        The new row's ``outbox_id`` (BIGSERIAL).

    Raises:
        DuplicateEnqueueError: ``idempotency_key`` already exists.
        TenantContextRequired: ``tenant_id`` is empty.
        TypeError: ``action`` is not a dict.
        ValueError: ``idempotency_key`` is empty.
    """
    if not isinstance(action, dict):
        raise TypeError(
            f"action must be a dict (JSONB), got {type(action).__name__}"
        )
    key = str(idempotency_key).strip()
    if not key:
        raise ValueError(
            "idempotency_key must be a non-empty string"
        )

    with tenant_connection(tenant_id, dsn=dsn) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"""
                    INSERT INTO {TABLE_NAME}
                        (tenant_id, action, idempotency_key)
                    VALUES
                        (%s, %s::jsonb, %s)
                    RETURNING outbox_id
                    """,
                    (str(tenant_id), json.dumps(action), key),
                )
                row = cur.fetchone()
            except psycopg.errors.UniqueViolation as exc:
                # The UNIQUE constraint on idempotency_key is the dedup
                # primitive. Surface it as a typed exception so the caller
                # can distinguish a replay from any other failure.
                raise DuplicateEnqueueError(key) from exc
    assert row is not None  # RETURNING guarantees a row
    return int(row[0])


def pending(
    limit: int = 100,
    *,
    tenant_id: UUID | str | None = None,
    dsn: str | None = None,
) -> list[OutboxRow]:
    """Return up to ``limit`` unapplied rows in outbox_id order.

    The consumer always reads in outbox_id order so the cascade replays
    actions in the order they were enqueued — important when one action
    reads a file that a later action overwrites.

    Args:
        limit:     Maximum rows to return. Default 100 (the AC default).
                   Caller MUST clamp to a sane bound; this function does
                   NOT clamp to avoid hiding unbounded-read bugs.
        tenant_id: If supplied, scope the query to one tenant via RLS.
                   If ``None``, use an admin connection (BYPASSRLS) and
                   return rows across all tenants — operator-only path.
        dsn:      Optional DSN override.

    Returns:
        A list of :class:`OutboxRow` ordered by ``outbox_id`` ASC.
    """
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError(
            f"limit must be a positive integer, got {limit!r}"
        )

    # Two paths: tenant-scoped (uses DEFAULT_APP_ROLE_DSN + RLS) or admin
    # (uses DEFAULT_DSN — orchestrator superuser with BYPASSRLS — for the
    # operator-facing metrics query in regenerate_metrics.py).
    if tenant_id is None:
        with _connect(dsn or DEFAULT_DSN) as conn:
            rows = _fetch_pending(conn, limit)
    else:
        with tenant_connection(tenant_id, dsn=dsn) as conn:
            rows = _fetch_pending(conn, limit)
    return rows


def _fetch_pending(conn: psycopg.Connection, limit: int) -> list[OutboxRow]:
    """Shared read path for :func:`pending` — both tenant and admin modes
    run this identical SELECT. The RLS policy (or BYPASSRLS, for admin)
    is what enforces the scope, not the SQL itself."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                outbox_id,
                tenant_id,
                action,
                idempotency_key,
                created_at,
                applied_at,
                applied_by
              FROM {TABLE_NAME}
             WHERE applied_at IS NULL
             ORDER BY outbox_id ASC
             LIMIT %s
            """,
            (limit,),
        )
        raw = cur.fetchall()
    return [
        OutboxRow(
            outbox_id=int(r[0]),
            tenant_id=r[1] if isinstance(r[1], UUID) else UUID(str(r[1])),
            action=r[2] if isinstance(r[2], dict) else (
                json.loads(r[2]) if isinstance(r[2], (str, bytes)) else dict(r[2])
            ),
            idempotency_key=str(r[3]),
            created_at=r[4],
            applied_at=r[5],
            applied_by=(None if r[6] is None else str(r[6])),
        )
        for r in raw
    ]


def mark_applied(
    outbox_id: int,
    applied_by: str,
    *,
    dsn: str | None = None,
) -> None:
    """Mark a single row as applied.

    Idempotent: calling this on a row whose ``applied_at`` is already set
    is a no-op (the UPDATE matches 0 rows but does not raise). The caller
    does NOT need to pre-check ``applied_at``.

    Args:
        outbox_id:  The BIGSERIAL PK of the row to mark.
        applied_by: A free-form identifier for who/what applied the row
                    (e.g. ``"orchestrator/outbox_consumer:worker-7c3a"``).
                    Stored verbatim; surfaced in
                    ``regenerate_metrics.py`` for forensic analysis.
        dsn:        Optional DSN override. Defaults to :data:`DEFAULT_DSN`
                    (the admin role with BYPASSRLS) — the orchestrator's
                    consumer process operates in admin context because
                    the orchestrator itself is single-tenant (the orchestrator
                    platform). Pass the tenant-scoped DSN only if you
                    have already set ``app.current_tenant`` on the
                    connection (see :func:`tenant_connection`).

    Raises:
        ValueError: ``outbox_id`` is not a positive int, or ``applied_by``
                    is empty.
    """
    if not isinstance(outbox_id, int) or isinstance(outbox_id, bool):
        raise ValueError(
            f"outbox_id must be a positive integer, got "
            f"{type(outbox_id).__name__}: {outbox_id!r}"
        )
    if outbox_id <= 0:
        raise ValueError(f"outbox_id must be positive, got {outbox_id}")
    who = str(applied_by).strip()
    if not who:
        raise ValueError("applied_by must be a non-empty string")

    # The query uses a WHERE clause that includes `applied_at IS NULL` so
    # a duplicate mark_applied is a silent no-op. This is the canonical
    # idempotency primitive on the consumer side: the same idempotency_key
    # can never be applied twice because (a) the consumer skips already-
    # applied rows in `pending()` (applied_at IS NULL filter) and (b) even
    # if it didn't, mark_applied would no-op.
    #
    # We default to ``DEFAULT_DSN`` (admin role, BYPASSRLS) because the
    # orchestrator's consumer process is single-tenant — there is no
    # caller-supplied tenant context to set on the connection. Future
    # multi-orchestrator deployments (per the project's decision records) would pass an
    # explicit ``dsn`` that has already had ``app.current_tenant`` set.
    with _connect(dsn or DEFAULT_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET applied_at = now(),
                       applied_by = %s
                 WHERE outbox_id = %s
                   AND applied_at IS NULL
                """,
                (who, outbox_id),
            )


# ---------------------------------------------------------------------------
# Aggregate metrics — used by regenerate_metrics.py
# ---------------------------------------------------------------------------


def stats(
    *,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Return aggregate outbox stats for the metrics dashboard.

    Reads as the admin role (BYPASSRLS) so the operator can see the global
    backlog. Two counters:

      * ``pending_count``           — rows where ``applied_at IS NULL``.
      * ``applied_last_minute_count`` — rows that crossed to applied in
                                        the last 60 seconds (proxy for
                                        "applied/min").

    Both are written to a small JSON file by ``outbox_consumer.py`` and
    re-read by ``regenerate_metrics.py``. This function is the CANONICAL
    read path used by ``regenerate_metrics.py`` when ``ORCH_OUTBOX_STATS``
    is unset (the most common case — operators re-generate metrics without
    having a live consumer).

    Args:
        dsn: Optional DSN override (default: admin DSN — needs BYPASSRLS).

    Returns:
        A dict with ``pending_count`` (int) + ``applied_last_minute_count``
        (int). Both default to 0 if the table is empty or unreachable.

    Raises:
        OutboxError: the DB query failed.
    """
    try:
        with _connect(dsn or DEFAULT_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*) FILTER (WHERE applied_at IS NULL)
                            AS pending_count,
                        COUNT(*) FILTER (
                            WHERE applied_at >= NOW() - INTERVAL '60 seconds'
                        )
                            AS applied_last_minute_count
                      FROM {TABLE_NAME}
                    """
                )
                row = cur.fetchone()
    except Exception as exc:
        raise OutboxError(
            f"could not read outbox stats: {exc}"
        ) from exc
    if row is None:
        return {"pending_count": 0, "applied_last_minute_count": 0}
    return {
        "pending_count": int(row[0] or 0),
        "applied_last_minute_count": int(row[1] or 0),
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Tiny CLI for ad-hoc inspection (used by smoke tests + humans).

    Usage::

        # Print the pending backlog (admin context)
        python3 -m orchestrator.scripts.outbox pending

        # Print aggregate stats
        python3 -m orchestrator.scripts.outbox stats

    Exit codes:
        0 — success
        2 — usage error (unknown subcommand)
        3 — DB error
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd == "pending":
        limit = int(rest[0]) if rest else 100
        rows = pending(limit=limit)
        for r in rows:
            print(
                f"{r.outbox_id}\t{r.tenant_id}\t{r.idempotency_key}\t"
                f"{r.applied_at or '-'}"
            )
        return 0
    if cmd == "stats":
        s = stats()
        print(json.dumps(s, indent=2))
        return 0
    print(f"error: unknown subcommand {cmd!r}", file=sys.stderr)
    return 2


__all__ = [
    "DEFAULT_APP_ROLE_DSN",
    "DEFAULT_DSN",
    "DuplicateEnqueueError",
    "OutboxError",
    "OutboxRow",
    "REPO_ROOT",
    "TABLE_NAME",
    "TenantContextRequired",
    "enqueue",
    "mark_applied",
    "pending",
    "stats",
    "tenant_connection",
]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))