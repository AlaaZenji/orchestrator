#!/usr/bin/env python3
"""Terminal UI renderer for the Astra burn-queue orchestrator.

ANSI-escape codes are used sparingly so output reads cleanly on both light
and dark terminals. Box-drawing characters are stdlib (no Unicode fancy).

Public functions (all `print` to stdout):
  - header(title, version, mode, stage_total, stage_idx)
  - stage_panel(title, num, total, summary_rows, claim_rows, skip_rows, drift_rows)
  - ticket_progress_bar(ticket_id, percent, status_emoji, label)
  - bottleneck_row(idx, kind, ticket_id, summary, auto_filed)
  - final_summary(claimed, completed, skipped, blocked, drift_count, blockers_count)
  - stage_divider()

Color scheme (using ANSI 38;5; for broad terminal support):
  - CYAN    (38;5;51)   → stage headers, neutral info
  - GREEN   (38;5;42)   → success / DONE
  - YELLOW  (38;5;220)  → warnings, skipped
  - RED     (38;5;196)  → blockers, drift, errors
  - MAGENTA (38;5;213)  → auto-filed (orchestrator action)
  - DIM     (38;5;240)  → secondary info

Reversibility: every call uses opt-in `enabled` flag (default True). Set
enabled=False to disable colors + box-drawing for logfile / CI contexts.
"""
from __future__ import annotations

import sys
import shutil
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

_ENABLED = True


def set_enabled(enabled: bool) -> None:
    global _ENABLED
    _ENABLED = enabled


def _esc(code: str) -> str:
    return code if _ENABLED else ""


def _color(text: str, code: str) -> str:
    """Wrap text in ANSI escape `code` (e.g., '38;5;51'). No-op when disabled."""
    if not _ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def _cyan(s: str) -> str:    return _color(s, "38;5;51")
def _green(s: str) -> str:   return _color(s, "38;5;42")
def _yellow(s: str) -> str:  return _color(s, "38;5;220")
def _red(s: str) -> str:     return _color(s, "38;5;196")
def _magenta(s: str) -> str: return _color(s, "38;5;213")
def _dim(s: str) -> str:     return _color(s, "38;5;240")
def _bold(s: str) -> str:    return _color(s, "1")


# ---------------------------------------------------------------------------
# Width
# ---------------------------------------------------------------------------

def terminal_width(default: int = 90) -> int:
    """Return the terminal width (with floor at `default`)."""
    try:
        return max(shutil.get_terminal_size((default, 20)).columns, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------

def rule(width: Optional[int] = None, char: str = "─", color=_dim) -> None:
    """Print a single horizontal rule spanning the terminal width."""
    w = width or terminal_width()
    print(color(char * w))


def header(
    title: str = "ASTRA BURN QUEUE ORCHESTRATOR",
    mode: str = "/burnqueue-all",
    started_at_iso: str = "",
    stage_label: str = "Stage 1/2 — Burn Queue",
    show_progress_bar: bool = True,
) -> None:
    w = terminal_width()
    bar_w = 30
    label_w = w - bar_w - 14
    if show_progress_bar:
        bar = "█" * int(bar_w * 0.05) + "░" * (bar_w - int(bar_w * 0.05))
    else:
        bar = ""
    print(_bold(_cyan("╔" + "═" * (w - 2) + "╗")))
    print(_bold(_cyan("║")) + _bold(_cyan(title.center(w - 2))) + _bold(_cyan("║")))
    print(_bold(_cyan("║")) + _dim(f"  Mode:    {mode}".ljust(w - 4)) + _bold(_cyan("║")))
    print(_bold(_cyan("║")) + _dim(f"  Started: {started_at_iso}".ljust(w - 4)) + _bold(_cyan("║")))
    print(_bold(_cyan("║")) + _dim(f"  Stage:   [{bar}] {stage_label}".ljust(w - 4)) + _bold(_cyan("║")))
    print(_bold(_cyan("╚" + "═" * (w - 2) + "╝")))


def stage_panel(
    stage_num: int,
    stage_total: int,
    title: str,
    initial_tickets: int,
    claimed: int,
    completed: int,
    skipped: int,
    blocked: int,
    in_progress: int,
    claim_rows: list[tuple[str, str, str]],
    skip_rows: list[tuple[str, str, str]],
    drift_rows: Optional[list[tuple[str, str, str]]] = None,
) -> None:
    """Render a stage panel with summary stats + ticket tables.

    Args:
        claim_rows: list of (ticket_id, percent_or_status_emoji, label)
                    examples: ("TKT-DEEP-LAUNCH-001", "████████░░ 80%", "in progress")
                              ("TKT-DEEP-CDS-005",      "██████████ DONE", "completed")
        skip_rows:  list of (ticket_id, "reason", "auto-filed-TICKET-ID")
    """
    w = terminal_width()
    bar_w = 22
    bar = _progress_bar(claimed + completed, initial_tickets, bar_w) if initial_tickets else ""
    print()
    print(_bold(_cyan("╔═══ STAGE ")) + _bold(_cyan(f"{stage_num}/{stage_total}: {title.upper()}")) + _bold(_cyan("═" * max(2, w - 32 - len(title))) + "╗"))
    print(_bold(_cyan("║")) + _dim(f"  [{bar}]").ljust(w - 4) + _bold(_cyan("║")))
    print(_bold(_cyan("║")) + _dim("").ljust(w - 4) + _bold(_cyan("║")))
    # Summary rows
    pct = f"{(completed + claimed) / max(initial_tickets, 1) * 100:.0f}%"
    rows = [
        ("Initial tickets", str(initial_tickets)),
        ("Claimed", f"{_green(str(claimed))}"),
        ("Completed", f"{_green(str(completed))}"),
        ("In progress", f"{_yellow(str(in_progress))}"),
        ("Skipped", f"{_yellow(str(skipped))}"),
        ("Blocked", f"{_red(str(blocked))}"),
    ]
    for label, val in rows:
        line = f"  {label:<18} {val}"
        print(_bold(_cyan("║")) + _dim(line).ljust(w - 4) + _bold(_cyan("║")))
    print(_bold(_cyan("║")) + _dim("").ljust(w - 4) + _bold(_cyan("║")))

    # Claimed / in-progress tickets
    if claim_rows:
        print(_bold(_cyan("║")) + _bold("  ⊕  Claimed / In progress").ljust(w - 4) + _bold(_cyan("║")))
        for tid, pct_or_status, label in claim_rows:
            line = f"     ⊢ {_dim(tid)}   {pct_or_status}  {_dim(label)}"
            print(_bold(_cyan("║")) + line.ljust(w - 4) + _bold(_cyan("║")))
        print(_bold(_cyan("║")) + _dim("").ljust(w - 4) + _bold(_cyan("║")))

    # Skipped tickets
    if skip_rows:
        print(_bold(_cyan("║")) + _bold("  ⊖  Skipped (with reason)").ljust(w - 4) + _bold(_cyan("║")))
        for tid, reason, auto_filed in skip_rows:
            print(_bold(_cyan("║")) + f"     ⊢ {_yellow(tid)}".ljust(w - 4) + _bold(_cyan("║")))
            print(_bold(_cyan("║")) + f"       reason: {reason}".ljust(w - 4) + _bold(_cyan("║")))
            if auto_filed and auto_filed != "-":
                print(_bold(_cyan("║")) + f"       {_magenta('auto-filed:')} {auto_filed}".ljust(w - 4) + _bold(_cyan("║")))
        print(_bold(_cyan("║")) + _dim("").ljust(w - 4) + _bold(_cyan("║")))

    # Drift rows (Stage 2)
    if drift_rows:
        print(_bold(_cyan("║")) + _bold(_red("  ⚠  Drift findings")).ljust(w - 4) + _bold(_cyan("║")))
        for tid, summary, auto_filed in drift_rows:
            print(_bold(_cyan("║")) + f"     ⊢ {_red(tid)}".ljust(w - 4) + _bold(_cyan("║")))
            print(_bold(_cyan("║")) + f"       drift: {summary}".ljust(w - 4) + _bold(_cyan("║")))
            if auto_filed and auto_filed != "-":
                print(_bold(_cyan("║")) + f"       {_magenta('auto-filed:')} {auto_filed}".ljust(w - 4) + _bold(_cyan("║")))
        print(_bold(_cyan("║")) + _dim("").ljust(w - 4) + _bold(_cyan("║")))

    print(_bold(_cyan("╚" + "═" * (w - 2) + "╝")))


def final_summary(
    total_tickets: int,
    completed: int,
    skipped: int,
    auto_filed: int,
    duration_seconds: float,
    bottlenecks_resolved: int,
    bottlenecks_remaining: int,
) -> None:
    w = terminal_width()
    print()
    print(_bold(_green("╔" + "═" * (w - 2) + "╗")))
    print(_bold(_green("║")) + _bold(_green("✓ BURN QUEUE ALL — COMPLETE".center(w - 2))) + _bold(_green("║")))
    print(_bold(_green("╚" + "═" * (w - 2) + "╝")))
    print()
    print(_bold("  Summary"))
    print(_dim("  " + "─" * 50))
    print(f"    Total tickets seen           {total_tickets:>4}")
    print(f"    Completed (DONE)              {_green(str(completed)):>4}")
    print(f"    Skipped (gate/parse issues)   {_yellow(str(skipped)):>4}")
    print(f"    Auto-filed (from bottlenecks) {_magenta(str(auto_filed)):>4}")
    print(f"    Bottlenecks resolved          {bottlenecks_resolved:>4}")
    print(f"    Bottlenecks remaining         {bottlenecks_remaining:>4}")
    print(f"    Total time                    {duration_seconds:.1f}s")
    print()
    if bottlenecks_remaining == 0:
        print(_bold(_green("  ✓  All clear. Queue is healthy.")))
    else:
        print(_bold(_yellow(f"  ⚠  {bottlenecks_remaining} bottleneck(s) still pending. Run /burnqueue-all again.")))


def bottleneck_finding(kind: str, ticket_id: str, summary: str) -> None:
    """Render an in-line bottleneck finding notice during detection."""
    color = _red if kind == "BLOCKER" else _yellow
    icon = "✗" if kind == "BLOCKER" else "⚠"
    print(f"  {color(icon)} {color(kind)}: {_dim(ticket_id)} — {summary}")


def auto_filed_notice(ticket_id: str, summary: str) -> None:
    """Notice that a fix-ticket has been auto-filed for a finding."""
    print(f"      {_magenta('+ auto-filed')} {ticket_id} — {summary}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _progress_bar(done: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return "░" * width
    pct = max(0.0, min(1.0, done / total))
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {int(pct * 100):3d}%"
