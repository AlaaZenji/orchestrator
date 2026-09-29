"""Tests for outbox_consumer.py — the outbox consumer side of Step 6.

TKT-ORCH-FIX-STATE-SYNC-OUTBOX-CONSUMER (2026-09-29).

Coverage (per the task spec — at least 6 tests):

  1. test_single_row_applied_and_marked_applied
       Single commit_status row → state.commit called + outbox.mark_applied
       called with the right outbox_id.
  2. test_multiple_rows_applied_in_outbox_id_order
       Three rows (one of each type) → handlers called in outbox_id order.
  3. test_unknown_action_type_marked_applied
       Unknown action.type → warning logged + mark_applied called
       (does NOT loop forever).
  4. test_exception_in_apply_logged_and_marked_failed
       Handler raises → error logged + mark_applied NOT called for that
       row + result.rows_failed > 0.
  5. test_status_subcommand_prints_stats
       `outbox_consumer.py status` → outbox.stats() called + JSON printed.
  6. test_loop_subcommand_sleeps_and_retries_on_empty_pending
       `run --loop` with empty pending → consume_pending called twice
       (with a sleep between empty ticks).

All tests mock outbox.pending, outbox.mark_applied, outbox.stats,
state.commit, and auto_reconcile.append_cascade_row_to_index — no live
Postgres required.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

# Import the consumer + its dependencies. Importing outbox_consumer pulls
# in outbox, state, and auto_reconcile (the modules it dispatches to).
import orchestrator.scripts.outbox as outbox_mod  # noqa: E402
import orchestrator.scripts.outbox_consumer as oc  # noqa: E402
import orchestrator.scripts.state as state_mod  # noqa: E402
import orchestrator.scripts.auto_reconcile as auto_reconcile_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    outbox_id: int,
    action: dict,
    idempotency_key: str = "test-key",
    tenant_id: str = "00000000-0000-0000-0000-000000000000",
) -> outbox_mod.OutboxRow:
    """Build an OutboxRow with the minimum fields the consumer reads."""
    return outbox_mod.OutboxRow(
        outbox_id=outbox_id,
        tenant_id=tenant_id,  # OutboxRow expects UUID, but str is coerced
        action=action,
        idempotency_key=idempotency_key,
        created_at=None,
        applied_at=None,
        applied_by=None,
    )


# ---------------------------------------------------------------------------
# 1. Single row applied + marked applied
# ---------------------------------------------------------------------------


def test_single_row_applied_and_marked_applied():
    """Single commit_status row → state.commit + outbox.mark_applied."""
    row = _make_row(
        outbox_id=42,
        action={
            "type": "commit_status",
            "ticket_id": "TKT-FOO",
            "new_status": "DONE",
            "previous_status": "IN_PROGRESS",
        },
    )

    with patch.object(outbox_mod, "pending", return_value=[row]) as m_pending, \
         patch.object(outbox_mod, "mark_applied") as m_mark, \
         patch.object(state_mod, "commit") as m_commit, \
         patch.object(auto_reconcile_mod, "append_cascade_row_to_index") as m_append:
        result = oc.consume_pending(limit=100)

    # outbox.pending called once with the right limit.
    assert m_pending.call_count == 1
    assert m_pending.call_args.kwargs.get("limit") == 100

    # state.commit called once with the right args.
    assert m_commit.call_count == 1
    args, kwargs = m_commit.call_args
    assert args[0] == "TKT-FOO"  # ticket_id
    assert args[1] == "DONE"      # new_status
    assert kwargs["source"] == "outbox_consumer"
    # previous_status is forwarded as part of the reason for forensic traceability.
    assert "IN_PROGRESS" in kwargs["reason"]

    # auto_reconcile NOT called (commit_status doesn't touch tickets-index.md).
    assert m_append.call_count == 0

    # outbox.mark_applied called once with the right outbox_id +
    # applied_by identifier (applied_by is a keyword arg on
    # outbox.mark_applied).
    assert m_mark.call_count == 1
    assert m_mark.call_args.args[0] == 42
    assert m_mark.call_args.kwargs["applied_by"] == oc.DEFAULT_APPLIED_BY

    # Result counters are correct.
    assert result["rows_seen"] == 1
    assert result["rows_applied"] == 1
    assert result["rows_failed"] == 0
    assert result["rows_unknown"] == 0
    assert result["applied_outbox_ids"] == [42]
    assert result["failed"] == []


# ---------------------------------------------------------------------------
# 2. Multiple rows applied in outbox_id order
# ---------------------------------------------------------------------------


def test_multiple_rows_applied_in_outbox_id_order():
    """Three rows (one of each type) → handlers called in outbox_id order."""
    # NB: outbox.pending() returns rows in outbox_id ASC order; we model
    # that here so the test reflects the production read order.
    rows = [
        _make_row(
            outbox_id=10,
            action={
                "type": "commit_status",
                "ticket_id": "TKT-A",
                "new_status": "DONE",
                "previous_status": "IN_PROGRESS",
            },
            idempotency_key="k10",
        ),
        _make_row(
            outbox_id=11,
            action={
                "type": "append_cascade_row",
                "ticket_id": "TKT-A",
                "row_text": "| TKT-A | DONE | P0 | foo | 2026-09-29 | 2026-09-29 | x |\n",
            },
            idempotency_key="k11",
        ),
        _make_row(
            outbox_id=12,
            action={
                "type": "commit_status",
                "ticket_id": "TKT-B",
                "new_status": "DONE",
                "previous_status": "IN_PROGRESS",
            },
            idempotency_key="k12",
        ),
    ]

    with patch.object(outbox_mod, "pending", return_value=rows), \
         patch.object(outbox_mod, "mark_applied") as m_mark, \
         patch.object(state_mod, "commit") as m_commit, \
         patch.object(auto_reconcile_mod, "append_cascade_row_to_index") as m_append:
        result = oc.consume_pending(limit=100)

    # Both state.commit calls happened in outbox_id order.
    assert [c.args[0] for c in m_commit.call_args_list] == ["TKT-A", "TKT-B"]
    # auto_reconcile.append_cascade_row_to_index called once for outbox_id=11.
    assert m_append.call_count == 1
    assert m_append.call_args.args[0] == "TKT-A"

    # mark_applied called once per row in outbox_id order.
    assert [m.args[0] for m in m_mark.call_args_list] == [10, 11, 12]

    assert result["rows_seen"] == 3
    assert result["rows_applied"] == 3
    assert result["rows_failed"] == 0
    assert result["applied_outbox_ids"] == [10, 11, 12]


# ---------------------------------------------------------------------------
# 3. Unknown action type → log warning + mark applied (doesn't loop)
# ---------------------------------------------------------------------------


def test_unknown_action_type_marked_applied(caplog):
    """Unknown action.type → warning logged + mark_applied called."""
    row = _make_row(
        outbox_id=99,
        action={"type": "future_action_we_dont_know_about"},
    )

    with patch.object(outbox_mod, "pending", return_value=[row]), \
         patch.object(outbox_mod, "mark_applied") as m_mark, \
         patch.object(state_mod, "commit") as m_commit, \
         patch.object(auto_reconcile_mod, "append_cascade_row_to_index"):
        with caplog.at_level(logging.WARNING, logger="orchestrator.outbox_consumer"):
            result = oc.consume_pending(limit=100)

    # Warning was logged.
    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "unknown action.type" in r.getMessage()
    ]
    assert warning_records, f"expected an unknown-action warning, got: {[r.getMessage() for r in caplog.records]}"

    # No state.commit / auto_reconcile call for an unknown type.
    assert m_commit.call_count == 0

    # The row IS marked applied (so it doesn't loop forever on every tick).
    assert m_mark.call_count == 1
    assert m_mark.call_args.args[0] == 99

    # Result: applied=1, unknown=1, failed=0.
    assert result["rows_seen"] == 1
    assert result["rows_applied"] == 1
    assert result["rows_unknown"] == 1
    assert result["rows_failed"] == 0


# ---------------------------------------------------------------------------
# 4. Exception in apply → logged + still mark applied (well — NOT marked
#    applied in our impl; the row stays unapplied for the next tick to
#    retry). The spec wording is ambiguous; the implementation choice
#    is the safe one (don't mark applied if apply failed).
# ---------------------------------------------------------------------------


def test_exception_in_apply_logged_and_marked_failed(caplog):
    """Handler raises → error logged + mark_applied NOT called + result
    records the failure.

    The next tick will re-attempt the row (atomicity discipline — apply
    first, mark_applied second). This is the canonical behavior per the
    module docstring's "Atomicity discipline" section.
    """
    row = _make_row(
        outbox_id=7,
        action={
            "type": "commit_status",
            "ticket_id": "TKT-FAIL",
            "new_status": "DONE",
        },
    )

    boom = RuntimeError("simulated state.commit failure")

    with patch.object(outbox_mod, "pending", return_value=[row]), \
         patch.object(outbox_mod, "mark_applied") as m_mark, \
         patch.object(state_mod, "commit", side_effect=boom), \
         patch.object(auto_reconcile_mod, "append_cascade_row_to_index"):
        with caplog.at_level(logging.ERROR, logger="orchestrator.outbox_consumer"):
            result = oc.consume_pending(limit=100)

    # mark_applied NOT called — the row stays unapplied for the next tick.
    assert m_mark.call_count == 0

    # Error logged with the outbox_id + ticket_id + type.
    err_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "apply failed" in r.getMessage()
    ]
    assert err_records, f"expected an apply-failed error, got: {[r.getMessage() for r in caplog.records]}"
    msg = err_records[0].getMessage()
    assert "outbox_id=7" in msg
    assert "TKT-FAIL" in msg

    # Result: applied=0, failed=1, the failure is in the failed list.
    assert result["rows_seen"] == 1
    assert result["rows_applied"] == 0
    assert result["rows_failed"] == 1
    assert result["applied_outbox_ids"] == []
    assert len(result["failed"]) == 1
    f = result["failed"][0]
    assert f["outbox_id"] == 7
    assert f["ticket_id"] == "TKT-FAIL"
    assert f["type"] == "commit_status"
    assert "simulated state.commit failure" in f["error"]


# ---------------------------------------------------------------------------
# 5. Status subcommand prints stats
# ---------------------------------------------------------------------------


def test_status_subcommand_prints_stats(capsys):
    """`outbox_consumer.py status` → outbox.stats() called + JSON printed."""
    fake_stats = {"pending_count": 5, "applied_last_minute_count": 12}

    with patch.object(outbox_mod, "stats", return_value=fake_stats) as m_stats:
        rc_code = oc.main(["status"])

    # outbox.stats called once (no DSN override).
    assert m_stats.call_count == 1

    # Exit code 0 + JSON printed to stdout.
    assert rc_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == fake_stats


def test_status_subcommand_handles_db_failure(capsys):
    """`status` returns exit 3 + writes a graceful message when DB is down."""
    with patch.object(
        outbox_mod, "stats",
        side_effect=outbox_mod.OutboxError("connection refused"),
    ):
        rc_code = oc.main(["status"])

    assert rc_code == 3
    err = capsys.readouterr().err
    assert "DB unreachable" in err
    assert "connection refused" in err


# ---------------------------------------------------------------------------
# 6. Loop subcommand sleeps + retries on empty pending
# ---------------------------------------------------------------------------


def test_loop_subcommand_sleeps_and_retries_on_empty_pending(monkeypatch):
    """`run --loop` with empty pending → consume_pending called repeatedly
    + time.sleep called between empty ticks.

    We patch ``time.sleep`` to a no-op so the test is fast, and we use a
    ``should_stop``-style counter to break the loop after a few ticks.
    """
    call_count = {"n": 0}

    def fake_consume_pending(**kwargs):
        call_count["n"] += 1
        return {
            "rows_seen": 0,
            "rows_applied": 0,
            "rows_failed": 0,
            "rows_unknown": 0,
            "applied_outbox_ids": [],
            "failed": [],
        }

    sleep_count = {"n": 0}

    def fake_sleep(seconds):
        sleep_count["n"] += 1
        # Break the loop after 3 sleep calls so the test terminates.
        if sleep_count["n"] >= 3:
            raise RuntimeError("STOP_TEST")

    monkeypatch.setattr(oc, "consume_pending", fake_consume_pending)
    monkeypatch.setattr(oc.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="STOP_TEST"):
        oc.run_loop(limit=100, interval=0.1)

    # 3 empty ticks → 3 sleeps.
    assert sleep_count["n"] == 3
    # consume_pending called at least 3 times.
    assert call_count["n"] >= 3


# ---------------------------------------------------------------------------
# Bonus: --help works + argparse wiring is correct
# ---------------------------------------------------------------------------


def test_help_prints_module_docstring(capsys):
    """`--help` prints the parser help (argparse calls sys.exit(0))."""
    with pytest.raises(SystemExit) as exc_info:
        oc.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "outbox" in out.lower()


def test_unknown_subcommand_exits_2(capsys):
    """Unknown subcommand → argparse raises SystemExit(2)."""
    with pytest.raises(SystemExit) as exc_info:
        oc.main(["definitely-not-a-subcommand"])
    assert exc_info.value.code == 2


def test_commit_status_handler_forwards_fencing_and_holder():
    """The commit_status dispatcher forwards fencing_token + holder_id
    to state.commit when present in the action payload."""
    row = _make_row(
        outbox_id=1,
        action={
            "type": "commit_status",
            "ticket_id": "TKT-FWD",
            "new_status": "DONE",
            "previous_status": "IN_PROGRESS",
            "fencing_token": 123,
            "holder_id": "worker-7c3a",
            "tenant_id": "00000000-0000-0000-0000-000000000000",
        },
    )

    with patch.object(outbox_mod, "pending", return_value=[row]), \
         patch.object(outbox_mod, "mark_applied"), \
         patch.object(state_mod, "commit") as m_commit, \
         patch.object(auto_reconcile_mod, "append_cascade_row_to_index"):
        oc.consume_pending(limit=10)

    assert m_commit.call_count == 1
    kwargs = m_commit.call_args.kwargs
    assert kwargs["fencing_token"] == 123
    assert kwargs["holder_id"] == "worker-7c3a"
    assert kwargs["tenant_id"] == "00000000-0000-0000-0000-000000000000"
    assert kwargs["source"] == "outbox_consumer"


def test_append_cascade_row_handler_calls_auto_reconcile():
    """The append_cascade_row dispatcher forwards to auto_reconcile."""
    row = _make_row(
        outbox_id=2,
        action={
            "type": "append_cascade_row",
            "ticket_id": "TKT-CAS",
            "row_text": "| TKT-CAS | DONE | P0 | x | 2026-09-29 | 2026-09-29 | x |\n",
        },
    )

    with patch.object(outbox_mod, "pending", return_value=[row]), \
         patch.object(outbox_mod, "mark_applied"), \
         patch.object(state_mod, "commit") as m_commit, \
         patch.object(auto_reconcile_mod, "append_cascade_row_to_index") as m_append:
        oc.consume_pending(limit=10)

    # state.commit NOT called for append_cascade_row.
    assert m_commit.call_count == 0
    # auto_reconcile.append_cascade_row_to_index called with the right args.
    assert m_append.call_count == 1
    assert m_append.call_args.args[0] == "TKT-CAS"
    assert m_append.call_args.args[1].startswith("| TKT-CAS |")