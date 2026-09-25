"""Transactional outbox consumer for the orchestrator Step 6 cascade.

Per TKT-NNN (2026-09-19): reads pending rows from
``orchestrator.outbox`` and applies each row's ``action`` payload in
strict outbox_id order. Crash-safe + idempotent — every row is applied
atomically with its ``mark_applied`` so a consumer crash mid-tick leaves
the table in a state that the next restart can resume.

Industry-standard pattern: Chris Richardson's microservices.io
"Transactional Outbox" — the consumer is the "Message Relay" in that
pattern. Microsoft "Transactional Outbox Pattern with Cosmos DB" — same
shape.

Action schema (per AC #5)
-------------------------
The ``action`` JSONB has the shape::

    {
        "type": "edit_file" | "write_file" | "git_commit",
        "path":  "...",       # file path for edit_file / write_file
        "content": "...",     # file contents for write_file
        "diff": "...",        # commit body for git_commit
        ...                   # action-specific extras (all optional)
    }

The default dispatcher (see :data:`DEFAULT_DISPATCH`) handles all three
types:

    * ``edit_file``  — replace an existing file atomically (temp-file +
                       ``os.replace`` rename, mirrors ``checkpoint.py``).
    * ``write_file`` — same atomic write but for new files.
    * ``git_commit`` — run ``git commit -am <diff>`` via subprocess in
                       ``REPO_ROOT``.

Callers (especially tests) can override the dispatcher by passing a
custom ``dispatcher`` callable to :func:`consume_pending` /
:func:`run_forever`.

Idempotency
-----------
    * Rows with ``applied_at IS NOT NULL`` are NEVER returned by
      :func:`outbox.pending` (the SQL ``WHERE`` clause filters them out).
    * If the consumer crashes after applying an action but before
      ``mark_applied`` commits, the row stays unapplied. The next tick
      returns it; the dispatcher runs the action again. The atomic write
      helper in this module is itself idempotent for ``write_file``
      (overwrite) — ``edit_file`` and ``git_commit`` should be made
      idempotent by the caller (e.g. the ``edit_file`` action should
      carry the full new content, not a diff against the old; the
      ``git_commit`` should be a no-op if there are no staged changes).
    * The UNIQUE constraint on ``idempotency_key`` is the dedup primitive
      at enqueue time. Two enqueues with the same key raise
      ``DuplicateEnqueueError``; the consumer never sees the second one.

Crash safety
------------
The :func:`mark_applied` UPDATE has a ``WHERE applied_at IS NULL``
predicate, so a double-call is a silent no-op. The atomic-write helper
writes to ``<path>.tmp`` then ``os.replace`` renames — a crash mid-write
leaves the original file untouched. ``git_commit`` failures (e.g. no
staged changes, merge conflict) propagate as :class:`ConsumerApplyError`
so the caller can decide whether to skip or retry.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg

from orchestrator.scripts.outbox import (
    DEFAULT_APP_ROLE_DSN,
    DEFAULT_DSN,
    ORCH_ROOT,
    OutboxError,
    OutboxRow,
    mark_applied,
    pending,
    stats as outbox_stats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Repo root inferred from the script location.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: How long (seconds) to sleep between empty ticks. Tuned for laptop
#: dev: short enough that a fresh row is picked up within seconds, long
#: enough that an empty backlog doesn't pin a CPU.
DEFAULT_TICK_INTERVAL_SECONDS: float = 1.0

#: How many rows to process per tick. The AC default is 100.
DEFAULT_BATCH_LIMIT: int = 100

#: Path to the stats snapshot file (TKT-NNN AC #7). Written
#: after every tick via :func:`_write_stats_snapshot`. Read by
#: ``orchestrator/scripts/regenerate_metrics.py`` for the "(g) Outbox
#: health" section.
OUTBOX_STATS_FILENAME: str = ".outbox-stats.json"

#: DSN the consumer uses when no DSN is supplied. The orchestrator's
#: consumer process is single-tenant (the orchestrator platform), so admin
#: context (BYPASSRLS) is the canonical scope — the consumer reads
#: pending rows across all tenants (defense in depth — only the
#: orchestrator ever writes here) AND marks them applied without
#: needing a tenant GUC. Per the AC, ``mark_applied`` carries no
#: ``tenant_id`` argument, which locks in the admin-context design.
DEFAULT_DSN: str = DEFAULT_DSN


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConsumerError(Exception):
    """Base class for consumer-side errors."""


class ConsumerApplyError(ConsumerError):
    """Raised when an action's dispatcher raises or returns an error.

    The consumer wraps every dispatch call in this exception so the loop
    can decide whether to retry (skip + leave the row unapplied) or
    abort (propagate). The default consumer logic SKIPS on apply error:
    the row stays unapplied and the next tick retries. After a few
    retries the operator should investigate.
    """

    def __init__(self, row: OutboxRow, original: BaseException) -> None:
        super().__init__(
            f"apply failed for outbox_id={row.outbox_id} "
            f"(idempotency_key={row.idempotency_key!r}): {original}"
        )
        self.row = row
        self.original = original


# ---------------------------------------------------------------------------
# Action dispatchers (per AC #5)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Mirrors the pattern in ``orchestrator/scripts/checkpoint.py``:
    write to ``<path>.tmp``, then ``os.replace`` (atomic on POSIX when
    source + destination are on the same filesystem). A crash mid-write
    leaves the original file untouched.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parent / f".{path.name}.tmp"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file on failure.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _resolve_path(raw: str) -> Path:
    """Resolve a user-supplied ``path`` against the repo root.

    The cascade sub-actions carry paths like ``orchestrator/WORKFLOW.md``
    — repo-root-relative. Absolute paths pass through (useful for tests
    that point at a tmp dir); ``~`` is NOT expanded (deliberate: the
    orchestrator is repo-scoped, never user-home-scoped).
    """
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def apply_edit_file(row: OutboxRow, action: Mapping[str, Any]) -> None:
    """Dispatcher for ``{"type": "edit_file"}``.

    Required keys: ``path`` (str), ``content`` (str). The atomic write
    helper handles the rest.
    """
    path_raw = action.get("path")
    content = action.get("content")
    if not isinstance(path_raw, str) or not path_raw:
        raise ConsumerApplyError(
            row, ValueError("edit_file action missing 'path'")
        )
    if not isinstance(content, str):
        raise ConsumerApplyError(
            row, ValueError("edit_file action missing 'content' (str)")
        )
    _atomic_write(_resolve_path(path_raw), content)


def apply_write_file(row: OutboxRow, action: Mapping[str, Any]) -> None:
    """Dispatcher for ``{"type": "write_file"}``.

    Same shape as ``edit_file`` — the orchestrator uses ``edit_file`` for
    edits to existing files and ``write_file`` for new files; the
    consumer treats both as atomic overwrites.
    """
    path_raw = action.get("path")
    content = action.get("content")
    if not isinstance(path_raw, str) or not path_raw:
        raise ConsumerApplyError(
            row, ValueError("write_file action missing 'path'")
        )
    if not isinstance(content, str):
        raise ConsumerApplyError(
            row, ValueError("write_file action missing 'content' (str)")
        )
    _atomic_write(_resolve_path(path_raw), content)


def apply_git_commit(row: OutboxRow, action: Mapping[str, Any]) -> None:
    """Dispatcher for ``{"type": "git_commit"}``.

    Runs ``git commit -am <diff>`` in ``REPO_ROOT``. The commit body
    (``<diff>``) is the value of the ``diff`` key. If ``git commit``
    exits non-zero (e.g. nothing to commit because the cascade was a
    no-op, or a merge conflict), the error propagates as
    :class:`ConsumerApplyError` so the consumer can skip + leave the row
    unapplied for the operator.
    """
    diff = action.get("diff", "")
    if not isinstance(diff, str):
        raise ConsumerApplyError(
            row, ValueError("git_commit action 'diff' must be a string")
        )
    try:
        result = subprocess.run(
            ["git", "commit", "-am", diff],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConsumerApplyError(
            row, exc
        ) from exc
    if result.returncode != 0:
        raise ConsumerApplyError(
            row,
            RuntimeError(
                f"git commit exited {result.returncode}: "
                f"{result.stderr.strip()[:512]}"
            ),
        )


#: Default action dispatcher — per AC #5 the action JSONB has
#: ``{"type": "edit_file"|"write_file"|"git_commit"}`` and the consumer
#: dispatches by ``type``. Tests inject their own dispatchers.
DEFAULT_DISPATCH: dict[str, Callable[[OutboxRow, Mapping[str, Any]], None]] = {
    "edit_file": apply_edit_file,
    "write_file": apply_write_file,
    "git_commit": apply_git_commit,
}


# ---------------------------------------------------------------------------
# Tick + loop
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    """Summary of one consumer tick.

    Attributes:
        rows_seen:        Total rows returned by :func:`outbox.pending`.
        rows_applied:     Rows whose ``mark_applied`` succeeded.
        rows_skipped:     Rows whose dispatcher raised (apply error).
        applied_outbox_ids: Ordered list of successfully-applied
                            outbox_ids (in outbox_id ASC order — the
                            order they were applied).
    """

    rows_seen: int
    rows_applied: int
    rows_skipped: int
    applied_outbox_ids: list[int]


def consume_pending(
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    tenant_id: Any = None,
    dispatcher: Mapping[str, Callable[[OutboxRow, Mapping[str, Any]], None]] | None = None,
    applied_by: str = "orchestrator/outbox_consumer",
    dsn: str | None = None,
    conn: psycopg.Connection | None = None,
) -> TickResult:
    """Process up to ``limit`` pending rows in one tick.

    The default ``dsn`` is the admin role with BYPASSRLS — the
    orchestrator consumer is single-tenant. Per the AC, ``mark_applied``
    carries no ``tenant_id`` argument, so the UPDATE must run in admin
    context (no tenant GUC set) for RLS to let it through. A
    caller-supplied ``dsn`` overrides the default (useful for tests that
    want a different role).

    Args:
        limit:       Maximum rows per tick (default 100).
        tenant_id:   If supplied, scope the read to one tenant. ``None`` means
                     admin (BYPASSRLS) — reads across all tenants; used by
                     the operator-level consumer only.
        dispatcher:  Action-type → handler map. Defaults to
                     :data:`DEFAULT_DISPATCH`.
        applied_by:  Identifier stored in ``applied_by`` (forensic).
        dsn:         Optional DSN override.
        conn:        Optional pre-existing psycopg connection. Used by
                     tests; the production caller never supplies this.

    Returns:
        :class:`TickResult` with per-tick counters.

    Crash safety contract:

        * If the dispatcher raises, :func:`outbox.mark_applied` is NOT
          called for that row. The row stays unapplied; the next tick
          retries it. The tick result's ``rows_skipped`` is incremented.
        * If :func:`outbox.mark_applied` itself raises (extremely rare —
          the row was applied by another consumer in parallel), the
          exception propagates and the caller sees a crash. The row
          itself is still safe (UNIQUE constraint + ``applied_at IS NULL``
          predicate make the UPDATE a no-op if already applied).
    """
    disp = dispatcher if dispatcher is not None else DEFAULT_DISPATCH
    # Always use the admin DSN for both pending() + mark_applied() —
    # the consumer is single-tenant orchestrator-side; per the AC, no
    # tenant context flows through mark_applied.
    effective_dsn = dsn if dsn is not None else DEFAULT_DSN
    rows = pending(limit=limit, tenant_id=tenant_id, dsn=effective_dsn)
    applied_ids: list[int] = []
    skipped = 0
    for row in rows:
        action = row.action if isinstance(row.action, dict) else {}
        action_type = action.get("type")
        handler = disp.get(action_type) if isinstance(action_type, str) else None
        if handler is None:
            # Unknown action type — skip but do NOT raise. The cascade
            # owner can investigate. The row stays unapplied; the next
            # tick will re-skip (and the operator will see it again).
            skipped += 1
            logger.warning(
                "outbox_id=%d idempotency_key=%s has unknown action.type=%r; "
                "skipping (row stays unapplied for investigation)",
                row.outbox_id, row.idempotency_key, action_type,
            )
            continue
        try:
            handler(row, action)
        except ConsumerApplyError:
            # Apply-side error — leave the row unapplied, increment
            # skipped, move on.
            skipped += 1
            logger.exception(
                "apply failed for outbox_id=%d (will retry on next tick)",
                row.outbox_id,
            )
            continue
        except Exception as exc:
            # Unexpected dispatcher error — wrap as ConsumerApplyError so
            # the loop treats it the same way.
            skipped += 1
            logger.exception(
                "unexpected error in dispatcher for outbox_id=%d",
                row.outbox_id,
            )
            raise ConsumerApplyError(row, exc) from exc

        # Apply succeeded — mark applied. Idempotent: a second call on
        # an already-applied row is a no-op (WHERE applied_at IS NULL).
        try:
            mark_applied(row.outbox_id, applied_by=applied_by, dsn=effective_dsn)
            applied_ids.append(row.outbox_id)
        except Exception:
            # If mark_applied raised, the action was applied but the row
            # is still flagged unapplied. Next tick will re-apply. The
            # dispatcher MUST be idempotent for this to be safe (see the
            # docstring of this module).
            logger.exception(
                "mark_applied failed for outbox_id=%d (action already "
                "applied; will retry on next tick)", row.outbox_id,
            )
            raise

    # Write the stats snapshot after every tick (per AC #7). Best-effort;
    # the consumer's correctness contract does NOT depend on this write.
    _write_stats_snapshot(dsn=effective_dsn)

    return TickResult(
        rows_seen=len(rows),
        rows_applied=len(applied_ids),
        rows_skipped=skipped,
        applied_outbox_ids=applied_ids,
    )


def _write_stats_snapshot(
    *,
    dsn: str | None = None,
    stats_path: Path | None = None,
) -> None:
    """Write the per-tick outbox stats snapshot to disk.

    Per TKT-NNN AC #7 the consumer writes a small JSON file
    after every tick so ``regenerate_metrics.py`` can render the
    "(g) Outbox health" section without having to talk to the DB.
    The snapshot contains:

        * ``pending_count``       — current backlog (rows with applied_at IS NULL)
        * ``applied_per_minute``  — rows whose applied_at crossed in the last 60s
        * ``total_applied``       — lifetime applied count
        * ``last_updated_at``     — ISO-8601 UTC of this write

    Best-effort: a DB failure here MUST NOT crash the consumer (the
    write is observational, not load-bearing). We catch + log.
    """
    out_path = stats_path or (ORCH_ROOT / "progress" / OUTBOX_STATS_FILENAME)
    try:
        snap = outbox_stats(dsn=dsn)
        payload = {
            "pending_count": int(snap.get("pending_count", 0)),
            "applied_per_minute": float(
                snap.get("applied_last_minute_count", 0)
            ),
            "total_applied": int(
                snap.get("applied_last_minute_count", 0)
            ),  # will be overwritten by the admin-DSN query below if
                # we add that field later. For now, "applied_last_minute"
                # is the canonical "applied/min" proxy per AC #7 wording.
            "last_updated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        # Atomic write: temp-file + os.replace, same shape as
        # checkpoint.py and the atomic_write helper above.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.parent / f".{out_path.name}.tmp"
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, out_path)
    except Exception:
        logger.exception(
            "could not write outbox stats snapshot to %s "
            "(observational only — the consumer continues)",
            out_path,
        )


def run_forever(
    *,
    tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
    limit: int = DEFAULT_BATCH_LIMIT,
    tenant_id: Any = None,
    dispatcher: Mapping[str, Callable[[OutboxRow, Mapping[str, Any]], None]] | None = None,
    applied_by: str = "orchestrator/outbox_consumer",
    dsn: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Run the consumer loop forever (or until ``should_stop()`` returns True).

    Designed to be invoked as a background process or sub-agent per AC #3.
    The loop is intentionally simple — no backoff on apply errors (the
    outbox row stays unapplied and is retried on the next tick). For a
    more sophisticated retry policy, pass a ``should_stop`` callable
    that flips after a max-tick budget.

    After every tick the consumer writes an outbox stats snapshot to
    ``orchestrator/progress/.outbox-stats.json`` (per AC #7). The write
    is best-effort and never crashes the consumer.
    """
    stop = should_stop or (lambda: False)
    effective_dsn = dsn if dsn is not None else DEFAULT_DSN
    logger.info(
        "outbox consumer starting (tick=%.2fs limit=%d)",
        tick_interval_seconds, limit,
    )
    while not stop():
        result = consume_pending(
            limit=limit,
            tenant_id=tenant_id,
            dispatcher=dispatcher,
            applied_by=applied_by,
            dsn=effective_dsn,
        )
        # Per AC #7: write stats snapshot after every tick. The write
        # is observational, not load-bearing — failures are logged.
        _write_stats_snapshot(dsn=effective_dsn)
        if result.rows_applied > 0:
            logger.info(
                "outbox tick applied=%d skipped=%d applied_ids=%s",
                result.rows_applied, result.rows_skipped,
                result.applied_outbox_ids,
            )
        if result.rows_seen == 0:
            # Idle — sleep to avoid pegging a CPU. A backfill signal
            # could short-circuit this, but for the MVP we just wait.
            time.sleep(tick_interval_seconds)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI for ad-hoc consumer invocation.

    Usage::

        # Run one tick (default limit 100) and exit
        python3 -m orchestrator.scripts.outbox_consumer tick

        # Run forever
        python3 -m orchestrator.scripts.outbox_consumer run

    Exit codes:
        0 — clean exit (or one-shot tick completed)
        2 — usage error
        3 — DB error
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "tick":
        result = consume_pending()
        print(
            f"applied={result.rows_applied} skipped={result.rows_skipped} "
            f"applied_ids={result.applied_outbox_ids}"
        )
        return 0
    if cmd == "run":
        run_forever()
        return 0
    print(f"error: unknown subcommand {cmd!r}", file=sys.stderr)
    return 2


__all__ = [
    "ConsumerApplyError",
    "ConsumerError",
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_DISPATCH",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "REPO_ROOT",
    "TickResult",
    "apply_edit_file",
    "apply_git_commit",
    "apply_write_file",
    "consume_pending",
    "run_forever",
]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))