#!/usr/bin/env python3
"""Verifier Doctrine Gate — mechanical enforcement of TKT-CORE-023 + TKT-DOCTRINE-001.

**Why this exists.**

The doctrine precedence update (TKT-CORE-023, 2026-09-26) made verifier dispatch
UNCONDITIONAL for P0/P1 tickets (8 distinct sub-agent IDs, Set semantics,
self-attestation FORBIDDEN). The validators exist at
`orchestrator/scripts/verifier_provenance.py` but **nothing in the dispatch
path calls them** — burn_queue.py, auto_reconcile.py, post_burn_audit.py
all skip the gate. Result: workers write `status: DONE` directly to the
ticket frontmatter without any programmatic check, and the orchestrator
catches self-attestation only when manually reviewing WORKER_RESULTs.

L113 burn (2026-09-28) demonstrated this gap: 2 of 3 workers returned
`final_status: DONE` with `verifier_provenance.dispatched_count: 0` and
self-attested all 8 lens verdicts inline. The orchestrator caught it via
post-hoc manual review — but the workers had ALREADY shipped DONE to disk.

**Fix.** This script is the single source of truth for ticket closure.
It reads every ticket with `status: DONE` in a layer, runs the doctrine
validators, and AUTOMATICALLY downgrades any ticket that fails. The
orchestrator's cascade_update step invokes this script before flipping
the layer. Workers CANNOT write `status: DONE` and have it stick — the
gate will roll it back.

**Modes:**

  - `validate --ticket-id TKT-XXX-NNN` — validate one ticket
  - `validate --layer N` — validate all DONE tickets in layer N
  - `validate --all-done` — validate every DONE ticket in tickets/
  - `validate --claimed-only` — only tickets with active leases
  - `apply` — same as validate but ALSO rewrites the ticket frontmatter
              (downgrades rejected tickets from DONE → PARTIAL_WITH_FOLLOW_UPS)

**Exit codes:**
  - 0 — every ticket passed all doctrine checks
  - 1 — at least one ticket was rejected (in apply mode, also rewritten)
  - 2 — script error (file not found, parse failure, etc.)

**Stdlib-only.** The doctrine validators in verifier_provenance.py are
also stdlib-only; the gate is mechanical, not policy.

**Date:** 2026-09-28 (L113 fix).
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

# Local import — same directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from verifier_provenance import (
    ValidationResult,
    validate_verifier_provenance,
    validate_self_attestation_block,
    validate_verifier_id_format,
    validate_verifier_id_format_full,
    validate_loop_audit,
    validate_tests_added,
    validate_astra_test_slot,
    validate_astra_test_regression_flag,
    validate_test_quality_metrics,
    validate_property_tests_present,
    validate_bug_class_registered,
    REJ_OK,
    REJ_INSUFFICIENT_VERIFIER_DISPATCH_FOR_PRIORITY,
    REJ_SELF_ATTESTATION_FORBIDDEN,
    REJ_INVALID_VERIFIER_ID_FORMAT,
    REJ_SUSPICIOUS_ID_PATTERN,
    REJ_REUSED_VERIFIER_ID,
    REJ_VERDICT_SOURCE_NOT_IN_PROVENANCE,
    REJ_ASTRA_TESTS_SLOT_MISSING,
    REJ_ASTRA_TESTS_FAILED,
    REJ_ASTRA_TESTS_REGRESSION,
    REJ_TESTS_ADDED_EMPTY,
    REJ_TEST_QUALITY_METRICS_MISSING,
    REJ_PROPERTY_TESTS_REQUIRED,
    REJ_BUG_CLASS_REGISTERED_MISSING,
    REJ_LOOP_AUDIT_FAIL,
    P0_MIN_VERIFIERS,
    P1_MIN_VERIFIERS,
    P2_MIN_VERIFIERS,
    P3_MIN_VERIFIERS,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
TICKETS_DIR = ROOT / "orchestrator" / "tickets"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

logger = logging.getLogger("orchestrator.doctrine_gate")


@dataclass
class TicketDoctrineCheck:
    """The full doctrine-check result for a single ticket."""
    ticket_id: str
    ticket_path: pathlib.Path
    frontmatter: dict = field(default_factory=dict)
    priority: str = ""
    status: str = ""
    # validator results
    provenance_result: Optional[ValidationResult] = None
    id_format_result: Optional[ValidationResult] = None
    self_attest_result: Optional[ValidationResult] = None
    loop_audit_result: Optional[ValidationResult] = None
    tests_added_result: Optional[ValidationResult] = None
    astra_slot_result: Optional[ValidationResult] = None
    astra_regression_result: Optional[ValidationResult] = None
    test_quality_result: Optional[ValidationResult] = None
    property_tests_result: Optional[ValidationResult] = None
    bug_class_result: Optional[ValidationResult] = None
    # summary
    ok: bool = True
    rejection_codes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "ticket_path": str(self.ticket_path.relative_to(ROOT)),
            "priority": self.priority,
            "status": self.status,
            "ok": self.ok,
            "rejection_codes": self.rejection_codes,
            "messages": self.messages,
        }


# ---------------------------------------------------------------------------
# Frontmatter parser (best-effort)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a ticket file.

    Returns (frontmatter_dict, body). Uses PyYAML (stdlib-adjacent) for
    correct handling of nested dicts, lists, and multi-line strings.

    Tolerant of malformed YAML (unquoted colons in evidence_tie / etc.):
    falls back to a block-based line scanner that handles nested mappings
    even when full PyYAML fails on a sibling field with problematic text.

    Returns ({}, body) if the file has no frontmatter.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end]
    body = text[end + 4:]

    fm: dict = {}
    yaml_failed = False

    # Attempt 1: full PyYAML parse.
    try:
        import yaml  # type: ignore
        parsed = yaml.safe_load(fm_text) or {}
        if isinstance(parsed, dict):
            fm = parsed
    except Exception:
        yaml_failed = True

    # When YAML failed: PyYAML returned nothing useful. We need to fill in
    # BOTH top-level scalars AND nested blocks. The block scanner captures
    # nested mappings as raw text; the line scanner picks up top-level
    # scalars. Run BOTH when YAML failed; otherwise just run the block
    # scanner only if PyYAML didn't yield the expected nested blocks.
    if yaml_failed:
        # Block scanner.
        blocks = _scan_blocks(fm_text)
        try:
            import yaml
            for key, sub_text in blocks.items():
                if not sub_text:
                    continue
                try:
                    parsed_block = yaml.safe_load("__wrapper__:\n" + sub_text)
                except Exception:
                    fm[key] = sub_text  # store raw text as fallback
                    continue
                if isinstance(parsed_block, dict) and "__wrapper__" in parsed_block:
                    fm[key] = parsed_block["__wrapper__"]
        except ImportError:
            pass

        # Line scanner for top-level scalars — ALWAYS runs when YAML failed.
        for line in fm_text.splitlines():
            m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*?)\s*$", line)
            if m and not line.startswith((" ", "\t")):
                key = m.group(1)
                val = m.group(2).strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                fm[key] = val

    elif any(
        k not in fm
        for k in ("verification_block", "verification", "files_changed",
                  "files_created", "tests_added")
    ):
        # YAML succeeded but is missing the keys we need. Run block scanner
        # to fill in the missing blocks only (don't overwrite scalars).
        blocks = _scan_blocks(fm_text)
        try:
            import yaml
            for key, sub_text in blocks.items():
                if key in fm or not sub_text:
                    continue
                try:
                    parsed_block = yaml.safe_load("__wrapper__:\n" + sub_text)
                except Exception:
                    continue
                if isinstance(parsed_block, dict) and "__wrapper__" in parsed_block:
                    fm[key] = parsed_block["__wrapper__"]
        except ImportError:
            pass

    return fm, body


def _scan_blocks(fm_text: str) -> dict[str, str]:
    """Scan YAML-ish frontmatter and return a dict of top-level keys to
    their indented-block content (raw text). Handles the common case of
    nested mappings under fields like `verification:` / `verification_block:`.
    """
    lines = fm_text.splitlines()
    blocks: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*?)\s*$", line)
        if not m or line.startswith((" ", "\t")):
            i += 1
            continue
        key = m.group(1)
        same_line_value = m.group(2).strip()
        # Collect indented continuation lines.
        block_lines = []
        j = i + 1
        while j < len(lines):
            sub = lines[j]
            if sub.startswith((" ", "\t")):
                block_lines.append(sub)
                j += 1
            elif sub.strip() == "":
                block_lines.append("")
                j += 1
            else:
                break
        if block_lines:
            # Strip a single common indent (the first non-empty line's indent).
            non_empty = [ln for ln in block_lines if ln.strip()]
            if non_empty:
                common_indent = min(
                    len(ln) - len(ln.lstrip(" \t")) for ln in non_empty
                )
                dedented = [
                    ln[common_indent:] if ln.startswith(" " * common_indent) or ln.startswith("\t" * common_indent) else ln
                    for ln in block_lines
                ]
                blocks[key] = "\n".join(dedented).rstrip()
            else:
                blocks[key] = ""
        elif same_line_value:
            # Scalar top-level field — store as string for consistency.
            blocks.setdefault(key, "")
        i = j
    return blocks


def find_ticket_file(ticket_id: str) -> Optional[pathlib.Path]:
    """Locate the canonical ticket file for a ticket_id."""
    for d in [TICKETS_DIR, TICKETS_DIR / "orch-self", TICKETS_DIR / "discovered",
              TICKETS_DIR / "slice-1"]:
        if not d.is_dir():
            continue
        for sub in d.glob("**/*"):
            if sub.is_file() and sub.name.startswith(ticket_id) and sub.suffix == ".md":
                return sub
    return None


# ---------------------------------------------------------------------------
# Doctrine check (the gate's core)
# ---------------------------------------------------------------------------


def extract_provenance(fm: dict, body: str) -> dict:
    """Extract the verifier_provenance block from the ticket frontmatter.

    Looks under `verifier_provenance` (canonical) and falls back to a
    `verification.verifier_provenance` legacy location.
    """
    prov = fm.get("verifier_provenance")
    if isinstance(prov, dict):
        prov = dict(prov)  # copy
        prov["priority"] = fm.get("priority", "P0")
        return prov
    verif = fm.get("verification") or {}
    if isinstance(verif, dict):
        prov = verif.get("verifier_provenance")
        if isinstance(prov, dict):
            prov = dict(prov)
            prov["priority"] = fm.get("priority", "P0")
            return prov
    return {
        "dispatched_count": 0,
        "sub_agent_ids": [],
        "priority": fm.get("priority", "P0"),
    }


def extract_work_result(fm: dict, body: str) -> dict:
    """Extract the work_result-like shape from the ticket for AGITA validation.

    The ticket frontmatter stores file/test lists under several possible keys:
      - `files_changed:` (top-level)
      - `files_created:` (Worker 2+ convention — synonym)
      - `tests_added:` (top-level)
      - `verification_block.files_changed:` (L106+ convention)
      - `verification.files_created:` (Worker 2 convention)
      - `verification.files_changed:` (legacy)

    Merge them all into a single list. The AGITA slot validator needs to
    find the slot reference in EITHER `tests_added` OR `files_changed`
    (workers vary in which they use).
    """
    files_top = fm.get("files_changed", []) or fm.get("files_created", [])
    tests_top = fm.get("tests_added", [])
    vb = fm.get("verification_block") or {}
    if not isinstance(vb, dict):
        vb = {}
    verif = fm.get("verification") or {}
    if not isinstance(verif, dict):
        verif = {}

    files_vb = vb.get("files_changed", []) or vb.get("files_created", [])
    tests_vb = vb.get("tests_added", [])
    files_verif = (
        verif.get("files_changed", [])
        or verif.get("files_created", [])
    )
    tests_verif = verif.get("tests_added", [])

    def _as_list(x):
        if isinstance(x, list):
            return x
        if isinstance(x, str) and x:
            return [x]
        return []

    merged_files = (
        _as_list(files_top) + _as_list(files_vb) + _as_list(files_verif)
    )
    merged_tests = (
        _as_list(tests_top) + _as_list(tests_vb) + _as_list(tests_verif)
    )

    norm_files = []
    for entry in merged_files:
        if isinstance(entry, dict):
            norm_files.append(entry)
        elif isinstance(entry, str):
            norm_files.append({"path": entry, "summary": ""})

    norm_tests = []
    for entry in merged_tests:
        if isinstance(entry, dict):
            norm_tests.append(entry)
        elif isinstance(entry, str):
            norm_tests.append({"path": entry, "test_name": ""})

    # CRITICAL: include any AGITA slot reference from files_changed in tests_added.
    expected_slot_tail = "astra-tests/astra_ticket_tests/"
    for f in norm_files:
        path = f.get("path", "") if isinstance(f, dict) else ""
        if expected_slot_tail in path:
            already = any(
                expected_slot_tail in t.get("path", "")
                for t in norm_tests
                if isinstance(t, dict)
            )
            if not already:
                norm_tests.append({"path": path, "test_name": "AGITA slot (referenced in files_changed)"})

    # If the on-disk slot exists but the YAML parser missed it (because of
    # broken YAML in the verification block), inject it manually. The
    # AGITA slot is a real pytest file — count it as a test.
    ticket_id = fm.get("id", "")
    if ticket_id:
        slot_path = ROOT / "astra-tests" / "astra_ticket_tests" / f"{ticket_id}.py"
        if slot_path.exists():
            already = any(
                expected_slot_tail in t.get("path", "")
                for t in norm_tests
                if isinstance(t, dict)
            )
            if not already:
                norm_tests.append({
                    "path": str(slot_path.relative_to(ROOT)),
                    "test_name": "AGITA slot (on-disk detection)",
                })

    return {
        "files_changed": norm_files,
        "tests_added": norm_tests,
        "test_quality_metrics": vb.get("test_quality_metrics") or {},
        "verification": vb if vb else verif,
        "final_status": fm.get("status", "DONE"),
        "bug_class_registered": fm.get("bug_class_registered"),
    }


def _try_parse_yamlish(raw: str) -> object:
    """Best-effort parser for the YAML-ish frontmatter values.
    Returns the raw string if it's a simple scalar; tries JSON if it
    looks like a JSON literal."""
    if not raw:
        return ""
    stripped = raw.strip()
    if not stripped:
        return ""
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return raw


def check_ticket(ticket_path: pathlib.Path, gate_deployment_date: str = "2026-09-28") -> TicketDoctrineCheck:
    """Run the full doctrine check on one ticket.

    The gate is forward-looking by default: it only enforces the doctrine on
    tickets whose `landed_at` timestamp is >= `gate_deployment_date`. Tickets
    that landed before the gate was deployed (e.g., 2026-09-27 and earlier)
    were verified under the older doctrine and are NOT downgraded
    retroactively. This avoids breaking historical cascade deltas.

    Set `gate_deployment_date=""` to enforce on all tickets regardless of
    date (use only for testing / explicit retroactive audits).
    """
    # Default ticket_id from the filename stem. The frontmatter `id:` field
    # overrides this if present (canonical for tests + non-standard paths).
    ticket_id = ticket_path.stem
    # Validate against the canonical pattern (TKT-PREFIX-NNN-slug).
    m = re.match(r"^(TKT-[A-Z0-9][A-Za-z0-9_\-]*?)(?:-[a-z][a-z0-9\-]*)?$", ticket_id)
    if m:
        ticket_id = m.group(1)
    check = TicketDoctrineCheck(
        ticket_id=ticket_id,
        ticket_path=ticket_path,
    )

    try:
        text = ticket_path.read_text(errors="ignore")
    except OSError as e:
        check.ok = False
        check.rejection_codes.append("READ_ERROR")
        check.messages.append(f"failed to read ticket: {e}")
        return check

    fm, body = parse_frontmatter(text)
    check.frontmatter = fm
    check.priority = fm.get("priority", "P0")
    check.status = fm.get("status", "")

    # Override ticket_id with the frontmatter `id:` field if present.
    # Tests + special paths rely on this.
    fm_id = fm.get("id", "")
    if fm_id and isinstance(fm_id, str) and fm_id.startswith("TKT-"):
        check.ticket_id = fm_id

    # Skip non-DONE tickets — the gate only enforces on closure.
    if check.status != "DONE":
        return check

    # Forward-looking enforcement: skip tickets landed before gate_deployment_date.
    # The gate is a NEW discipline (deployed 2026-09-28). Tickets that landed
    # before that were verified under the older doctrine and must not be
    # retroactively downgraded (would break historical cascade deltas).
    #
    # Enforcement rule: only check tickets where ANY of these signals is set
    # to a date on or after the gate deployment date:
    #   - `landed_at:` (canonical)
    #   - `verification.completed_at:` (L106+ convention)
    #   - `commit_ref:` matching `no-git-repo:L<layer>:*` where layer >= 100
    # Tickets with NONE of these (or all before the cutoff) are skipped.
    if gate_deployment_date:
        landed_at = fm.get("landed_at", "")
        verif = fm.get("verification") or {}
        if not isinstance(verif, dict):
            verif = {}
        completed_at = verif.get("completed_at", "") if isinstance(verif, dict) else ""
        picked_up_at = fm.get("picked_up_at", "")
        commit_ref = fm.get("commit_ref", "")
        # Check commit_ref layer (e.g. "no-git-repo:L113:..." → 113)
        layer_match = re.match(r"^no-git-repo:L(\d+):", str(commit_ref))
        commit_layer = int(layer_match.group(1)) if layer_match else 0

        # Determine the most recent date signal available. Precedence:
        # landed_at > completed_at > picked_up_at. Any one being post-gate
        # makes the ticket post-gate (forward-looking enforcement).
        date_signals = []
        for v in (landed_at, completed_at, picked_up_at):
            if v:
                if hasattr(v, "date"):
                    date_signals.append(v.date().isoformat())
                elif isinstance(v, str):
                    date_signals.append(v[:10])
                else:
                    date_signals.append(str(v)[:10])
        # L113 = gate deployment layer (2026-09-28).
        # Tickets with commit_layer >= 113 are post-gate.
        if not date_signals and commit_layer < 113:
            # No date signals AND commit_ref is pre-gate — assume pre-gate.
            return check
        if date_signals:
            # If ANY date signal is >= gate deployment date, enforce.
            if max(date_signals) < gate_deployment_date:
                return check
            # Otherwise (max >= gate_deployment_date): enforce.
        # If only commit_layer signal: enforce if >= 113.

    # 1. provenance validation
    prov = extract_provenance(fm, body)
    prov["priority"] = check.priority
    check.provenance_result = validate_verifier_provenance(prov, ticket_frontmatter=fm)
    if not check.provenance_result.ok:
        check.ok = False
        check.rejection_codes.append(check.provenance_result.rejection_code)
        check.messages.append(f"provenance: {check.provenance_result.message}")

    # 2. self-attestation block (pre-flight)
    check.self_attest_result = validate_self_attestation_block(prov)
    if not check.self_attest_result.ok:
        check.ok = False
        if check.self_attest_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.self_attest_result.rejection_code)
        check.messages.append(f"self-attestation: {check.self_attest_result.message}")

    # 3. verifier ID format (catches fabrication)
    check.id_format_result = validate_verifier_id_format_full(
        prov, verdict_sources=_try_parse_yamlish(fm.get("verdict_sources", "") or "{}")
    )
    if not check.id_format_result.ok:
        check.ok = False
        if check.id_format_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.id_format_result.rejection_code)
        check.messages.append(f"id_format: {check.id_format_result.message}")

    # 4. loop audit (catches single-round self-attestation)
    retries_raw = fm.get("retries", "0")
    try:
        retries = int(retries_raw)
    except (TypeError, ValueError):
        retries = 0
    check.loop_audit_result = validate_loop_audit(prov, retries=retries, priority=check.priority)
    if not check.loop_audit_result.ok:
        check.ok = False
        if check.loop_audit_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.loop_audit_result.rejection_code)
        check.messages.append(f"loop_audit: {check.loop_audit_result.message}")

    # 5. AGITA tests_added
    work_result = extract_work_result(fm, body)
    check.tests_added_result = validate_tests_added(work_result)
    if not check.tests_added_result.ok:
        check.ok = False
        if check.tests_added_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.tests_added_result.rejection_code)
        check.messages.append(f"tests_added: {check.tests_added_result.message}")

    # 6. AGITA slot reference — on-disk check is the primary signal (more reliable
    # than parsing worker output from YAML frontmatter, which can have unquoted
    # colons etc.). The validator result is informational only.
    ticket_id = check.ticket_id
    slot_path = ROOT / "astra-tests" / "astra_ticket_tests" / f"{ticket_id}.py"
    slot_on_disk = slot_path.exists()

    check.astra_slot_result = validate_astra_test_slot(ticket_id, work_result)
    if not slot_on_disk:
        # File missing on disk — definitive REJECT regardless of validator result.
        check.ok = False
        if "ASTRA_TESTS_SLOT_MISSING" not in check.rejection_codes:
            check.rejection_codes.append("ASTRA_TESTS_SLOT_MISSING")
        check.messages.append(
            f"astra_slot_on_disk: file not found at {slot_path.relative_to(ROOT)} "
            f"(this is the canonical signal — the validator's WORKER_RESULT "
            f"shape check is informational only)"
        )
        if not check.astra_slot_result.ok:
            check.messages.append(
                f"astra_slot_validator: {check.astra_slot_result.message}"
            )
    elif not check.astra_slot_result.ok:
        # File on disk but validator failed (e.g., WORKER_RESULT shape missing
        # the slot reference). Informational — file existence is the truth.
        check.messages.append(
            f"astra_slot_info: file exists on disk at {slot_path.relative_to(ROOT)} "
            f"but validator reports shape mismatch — informational only. "
            f"Validator message: {check.astra_slot_result.message}"
        )

    # 7. AGITA regression flag (worker must report astra_tests_new_passed)
    check.astra_regression_result = validate_astra_test_regression_flag(work_result)
    if not check.astra_regression_result.ok:
        check.ok = False
        if check.astra_regression_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.astra_regression_result.rejection_code)
        check.messages.append(f"astra_regression: {check.astra_regression_result.message}")

    # 8. test_quality_metrics shape (only if tests_added non-empty).
    # Advisory only — the core gate is verifier dispatch (already covered by
    # check #1). test_quality_metrics is a newer discipline (TKT-CORE-TEST-
    # QUALITY-LENS-001, 2026-09-27) and many existing workers don't populate
    # it. Surface as INFO, don't block.
    check.test_quality_result = validate_test_quality_metrics(work_result)
    if not check.test_quality_result.ok:
        check.messages.append(
            f"test_quality_metrics (advisory): {check.test_quality_result.message} "
            f"— newer discipline (2026-09-27); not yet blocking the gate"
        )

    # 9. property_tests_present (when parser/serializer files changed)
    check.property_tests_result = validate_property_tests_present(work_result)
    if not check.property_tests_result.ok:
        check.ok = False
        if check.property_tests_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.property_tests_result.rejection_code)
        check.messages.append(f"property_tests: {check.property_tests_result.message}")

    # 10. bug_class_registered for BLOCKER/PARTIAL
    check.bug_class_result = validate_bug_class_registered(
        work_result, ticket_frontmatter=fm
    )
    if not check.bug_class_result.ok:
        check.ok = False
        if check.bug_class_result.rejection_code not in check.rejection_codes:
            check.rejection_codes.append(check.bug_class_result.rejection_code)
        check.messages.append(f"bug_class: {check.bug_class_result.message}")

    return check


def downgrade_ticket(check: TicketDoctrineCheck) -> bool:
    """Atomically downgrade a DONE ticket to PARTIAL_WITH_FOLLOW_UPS.

    TKT-CORE-FIX-STATE-CONSISTENCY: delegates to ``state.commit()`` so
    the downgrade goes through the SINGLE chokepoint — atomic
    frontmatter write + reconcile event enqueue + audit log row.

    Returns True if the file was modified, False if no-op.
    """
    if check.ok or check.status != "DONE":
        return False

    rejection_codes_str = ", ".join(check.rejection_codes) if check.rejection_codes else "unknown"
    applied_at = __import__('datetime').datetime.now(
        __import__('datetime').timezone.utc
    ).isoformat()

    # Parse the ticket id from the frontmatter so we can call state.commit.
    try:
        text = check.ticket_path.read_text(errors="ignore")
    except OSError:
        return False
    m = re.search(r"^id:\s*(TKT-[\w-]+)\s*$", text, re.MULTILINE)
    if not m:
        return False
    ticket_id = m.group(1)

    # Use the chokepoint. force=True because downgrade is a doctrine
    # rollback (DONE → PARTIAL_WITH_FOLLOW_UPS is NOT in the default
    # state machine — it's an admin override).
    try:
        import state as state_mod  # type: ignore
        state_mod.commit(
            ticket_id,
            "PARTIAL_WITH_FOLLOW_UPS",
            source="doctrine_gate",
            reason=(
                f"verifier_provenance invalid: rejection_codes="
                f"[{rejection_codes_str}]"
            ),
            force=True,  # downgrade is admin override
        )
    except Exception as exc:
        # If state.commit fails (e.g. ticket not found), fall through
        # to the legacy in-place write so the gate still functions.
        # The audit row + reconcile event will be missing, but the
        # next reconcile --rebuild will pick up the frontmatter change.
        logger.warning(
            "verifier_doctrine_gate.downgrade.commit_failed "
            "ticket_id=%s exc=%s — falling back to in-place write",
            ticket_id, exc,
        )
        _legacy_in_place_downgrade(check, applied_at, rejection_codes_str)
        return True

    # Also append the doctrine_rejections block to frontmatter body.
    # We do this AFTER state.commit (which already wrote status + updated).
    # state.commit keeps the body unchanged, so we can safely re-read and
    # append the doctrine block.
    _append_doctrine_block(check.ticket_path, applied_at, rejection_codes_str)
    return True


def _legacy_in_place_downgrade(
    check: TicketDoctrineCheck,
    applied_at: str,
    rejection_codes_str: str,
) -> None:
    """Legacy in-place downgrade (used only when state.commit fails).

    Kept as a safety net so the gate still functions when state.commit
    raises. The next ``auto_reconcile.py --rebuild`` will catch the
    missing audit row + missing reconcile event.
    """
    try:
        text = check.ticket_path.read_text(errors="ignore")
    except OSError:
        return
    new_text = re.sub(
        r"^(status:\s*)DONE\s*$",
        r"\1PARTIAL_WITH_FOLLOW_UPS",
        text, count=1, flags=re.MULTILINE,
    )
    check.ticket_path.write_text(new_text)


def _append_doctrine_block(
    path: pathlib.Path, applied_at: str, rejection_codes_str: str,
) -> None:
    """Append a `doctrine_rejections:` block to frontmatter body."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return
    if "doctrine_rejections:" in text:
        return
    doctrine_block = (
        f"doctrine_rejections:\n"
        f"  applied_by: orchestrator/scripts/verifier_doctrine_gate.py\n"
        f"  applied_at: {applied_at}\n"
        f"  rejection_codes: [{rejection_codes_str}]\n"
    )
    new_text = re.sub(
        r"^(---\s*\n)",
        r"\1" + doctrine_block,
        text, count=1, flags=re.MULTILINE,
    )
    if new_text != text:
        path.write_text(new_text)


# ---------------------------------------------------------------------------
# Layer / ticket discovery
# ---------------------------------------------------------------------------


def find_done_tickets_in_layer(layer: int) -> list[pathlib.Path]:
    """Find tickets modified on the day the given layer was burned.

    Heuristic: the layer number appears in the most-recent
    orchestrator/progress/YYYY-MM-DD-burn-queue-L<N>.md file.
    """
    progress_dir = ROOT / "orchestrator" / "progress"
    if not progress_dir.is_dir():
        return []
    candidates = list(progress_dir.glob(f"*-burn-queue-L{layer}*.md"))
    if not candidates:
        # Fallback: any ticket with status DONE modified in the last 24h.
        import datetime
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
        return [
            p for p in TICKETS_DIR.rglob("*.md")
            if p.is_file() and "/done/" not in str(p) and _file_mtime(p) >= cutoff
        ]
    layer_date = max(p.stat().st_mtime for p in candidates)
    import datetime
    cutoff = datetime.datetime.fromtimestamp(layer_date) - datetime.timedelta(hours=6)
    return [
        p for p in TICKETS_DIR.rglob("*.md")
        if p.is_file() and "/done/" not in str(p) and _file_mtime(p) >= cutoff
    ]


def _file_mtime(p: pathlib.Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def find_all_done_tickets() -> list[pathlib.Path]:
    """Find every ticket currently marked status: DONE."""
    out = []
    for p in TICKETS_DIR.rglob("*.md"):
        if not p.is_file() or "/done/" in str(p):
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if re.search(r"^status:\s*DONE\s*$", text, re.MULTILINE):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Validate tickets; do not modify files")
    p_validate.add_argument("--ticket-id", help="Single ticket to validate")
    p_validate.add_argument("--layer", type=int, help="Layer number to scope the scan")
    p_validate.add_argument("--all-done", action="store_true", help="All DONE tickets")
    p_validate.add_argument("--json", action="store_true", help="JSON output")

    p_apply = sub.add_parser("apply", help="Validate AND downgrade rejected tickets")
    p_apply.add_argument("--ticket-id", help="Single ticket to apply")
    p_apply.add_argument("--layer", type=int, help="Layer number to scope the scan")
    p_apply.add_argument("--all-done", action="store_true", help="All DONE tickets")
    p_apply.add_argument("--dry-run", action="store_true", help="Report only; do not write")

    args = parser.parse_args()

    # Discover tickets.
    paths: list[pathlib.Path] = []
    if args.ticket_id:
        path = find_ticket_file(args.ticket_id)
        if path is None:
            print(f"ERROR: ticket {args.ticket_id} not found under {TICKETS_DIR}", file=sys.stderr)
            return 2
        paths.append(path)
    elif args.layer is not None:
        paths = find_done_tickets_in_layer(args.layer)
    elif args.all_done:
        paths = find_all_done_tickets()
    else:
        print("ERROR: must specify --ticket-id, --layer, or --all-done", file=sys.stderr)
        return 2

    if not paths:
        print("No tickets matched.")
        return 0

    # Run.
    results: list[TicketDoctrineCheck] = []
    downgraded: list[pathlib.Path] = []
    for p in paths:
        c = check_ticket(p)
        results.append(c)
        if args.cmd == "apply" and not c.ok and c.status == "DONE":
            if not args.dry_run:
                if downgrade_ticket(c):
                    downgraded.append(p)

    # Report.
    if args.cmd == "validate" and args.json:
        print(json.dumps([c.to_dict() for c in results], indent=2, default=str))
        return 0 if all(c.ok for c in results) else 1

    # Human-readable report.
    n_pass = sum(1 for c in results if c.ok or c.status != "DONE")
    n_fail = sum(1 for c in results if not c.ok and c.status == "DONE")
    print(f"Verifier Doctrine Gate — scanned {len(results)} ticket(s): "
          f"{n_pass} pass / {n_fail} fail")
    if args.cmd == "apply":
        if args.dry_run:
            print(f"  (dry-run) {n_fail} ticket(s) would be downgraded")
        else:
            print(f"  downgraded {len(downgraded)} ticket(s) from DONE → PARTIAL_WITH_FOLLOW_UPS")
            for p in downgraded:
                print(f"    - {p.relative_to(ROOT)}")
    if n_fail:
        print()
        print("Rejected tickets:")
        for c in results:
            if not c.ok and c.status == "DONE":
                print(f"  - {c.ticket_id} ({c.priority}): {c.rejection_codes}")
                for msg in c.messages[:5]:
                    print(f"      {msg}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())