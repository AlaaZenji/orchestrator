"""Verdict-conflict resolution — TKT-NNN (2026-09-22).

**Purpose.** Per the verification cadence doctrine (TKT-DOCTRINE-001, 2026-09-19), the
orchestrator loops a ticket until all dispatched verifier lenses PASS,
capped at 3 retries — then §Step 7 trigger #4 fires. Layer 29-30 evidence
(TKT-NNNb, TKT-NNNb-FOUND-003) shows that pattern is **wrong** when
a FAIL surfaces **cross-cutting substrate drift OUTSIDE the ticket's
call-site scope** — calling those failures "implementation defects to loop
until PASS" would silently ship broken runtime.

This module formalizes the resolution between the two conflict categories:

  (a) **Implementation defect FAIL** — the verifier found a real defect
      in code the ticket's AC owns (evidence file:line is INSIDE the
      ticket's scope). Worker re-implements; re-verify. Loop until PASS,
      capped at 3 retries (§Step 7 trigger #4).

  (b) **Substrate drift FAIL** — the verifier found a gap in code that
      lives OUTSIDE the ticket's AC (evidence file:line is OUTSIDE the
      ticket's scope, and follow_up_ticket_ids are present). Worker
      closes the call-site work; the substrate gap is filed as one or
      more follow-up tickets. The ticket transitions to PARTIAL (not
      DONE) — DO NOT loop.

The orchestrator calls `classify_verdict_failure()` once per failing
lens to decide what the worker should do next.

**Scope-label vocabulary (closed set, 3 axes — ticket-scope relation only):**

  - `inside_ticket_scope`  — failed file:line is in this ticket's AC.
                             → LOOP_UNTIL_PASS (existing behavior).
  - `outside_ticket_scope` — failed file:line is in code the ticket
                             doesn't own (cross-cutting substrate).
                             → PARTIAL_WITH_FOLLOW_UPS if follow-ups filed,
                             else AMBIGUOUS.
  - `unknown`              — scope cannot be determined (empty evidence,
                             no follow-ups). → AMBIGUOUS — orchestrator
                             surfaces per §Step 7.

The 3-axis collapse (vs a 5+ axis vocabulary like `substrate` /
`cross_cutting` / `pre_existing`) is deliberate — it picks ONE primary
dimension (ticket-scope relation) and demotes the others to optional
provenance metadata on the `evidence[].file:line` or the
`scope_difference_note` field. A FAIL can be `substrate + cross_cutting
+ pre_existing` (Layer 29 case) — those words describe WHAT failed;
what matters for resolution is WHETHER the failure is in this ticket's
AC.

**Conflict arbitration (n-way, `arbitrate`):**

  - If ANY verdict is FAIL with `outside_ticket_scope` + `follow_up_ticket_ids`
    → PARTIAL_WITH_FOLLOW_UPS (the thorough FAIL overrides narrow PASS).
  - If all verdicts agree → LOOP_UNTIL_PASS only if all PASS; AMBIGUOUS if all FAIL.
  - If the mix is FAIL-with-no-evidence + PASS-with-no-evidence → AMBIGUOUS.

**Schema (`verdict_conflict` on WORKER_RESULT.verification — see CONVENTIONS.md):**

    {
      "first_verdict":         "PASS" | "FAIL",
      "second_verdict":        "PASS" | "FAIL",
      "scope_difference_note": "<free text — what scopes the two verdicts cover>",
      "chosen_resolution":     "LOOP_UNTIL_PASS" | "PARTIAL_WITH_FOLLOW_UPS" | "AMBIGUOUS",
      "rationale":             "<free text — why the chosen_resolution is right>",
    }

The field is **optional** — only set when the worker had to break a tie
between two verifier dispatches. If both dispatches agree, no conflict
→ field absent.

**Stdlib-only.**

**Date.** 2026-09-22.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --- Constants --------------------------------------------------------------

# Stable resolution codes (mirror in CONVENTIONS.md).
RESOLUTION_LOOP_UNTIL_PASS = "LOOP_UNTIL_PASS"
RESOLUTION_PARTIAL_WITH_FOLLOW_UPS = "PARTIAL_WITH_FOLLOW_UPS"
RESOLUTION_AMBIGUOUS = "AMBIGUOUS"

# Closed set of scope labels — the 3-axis ticket-scope relation.
SCOPE_INSIDE = "inside_ticket_scope"
SCOPE_OUTSIDE = "outside_ticket_scope"
SCOPE_UNKNOWN = "unknown"

SCOPE_LABELS: frozenset[str] = frozenset({SCOPE_INSIDE, SCOPE_OUTSIDE, SCOPE_UNKNOWN})

# Stable rejection codes (mirror validator pattern in verifier_provenance.py).
REJ_OK = "OK"
REJ_SCOPE_LABEL_UNKNOWN = "SCOPE_LABEL_UNKNOWN"
REJ_FOLLOW_UP_TICKETS_REQUIRED = "FOLLOW_UP_TICKETS_REQUIRED"
REJ_VERDICT_CONFLICT_MALFORMED = "VERDICT_CONFLICT_MALFORMED"
REJ_VERDICT_NOT_FAIL = "VERDICT_NOT_FAIL"
REJ_INPUT_NOT_DICT = "INPUT_NOT_DICT"
REJ_SCOPE_LABEL_OUTSIDE_BUT_EVIDENCE_INSIDE = "SCOPE_LABEL_OUTSIDE_BUT_EVIDENCE_INSIDE"
REJ_INVALID_FOLLOW_UP_TICKET_ID = "INVALID_FOLLOW_UP_TICKET_ID"

# Lightweight ticket-ID format check. Production ticket IDs follow
# the `TKT-<PHASE>-<NNN>(-<slug>)*` shape per CONVENTIONS.md §File naming:
#   - `TKT-NNN`                       (basic)
#   - `TKT-NNNb`                      (R1 retry — letter suffix)
#   - `TKT-NNNb-FOUND-001`            (follow-up ticket from P2-001b)
#   - `TKT-PERF-001b-sub-LRU`            (follow-up with descriptive slug)
#   - `TKT-NNN-R1-retry-with-descriptive-slug` (R1 retry with descriptive slug)
# The shape is "TKT-" + at least 3 segments separated by "-", where each
# segment is `[a-zA-Z0-9-]+`. Defense-in-depth — the ticket factory
# enforces the canonical format, but the resolver re-checks before
# forwarding IDs to the cascade update.
_TICKET_ID_RE = __import__("re").compile(
    r"^TKT-[A-Za-z0-9]+-[A-Za-z0-9]+(-[A-Za-z0-9-]+)*$"
)


def _is_valid_ticket_id(tid: object) -> bool:
    """True iff tid is a string matching the canonical ticket-ID format."""
    if not isinstance(tid, str) or not tid:
        return False
    return bool(_TICKET_ID_RE.match(tid))


# --- Result type -------------------------------------------------------------

@dataclass
class ResolutionResult:
    """Outcome of a single verdict-conflict resolution.

    `ok=True` means the resolver reached a clean decision (LOOP / PARTIAL).
    `ok=False` means the resolver is AMBIGUOUS — the orchestrator must
    decide (typically surface per §Step 7 trigger #5).

    Never raises for AMBIGUOUS — the orchestrator decides what to do.
    """
    ok: bool
    chosen_resolution: str = RESOLUTION_AMBIGUOUS
    rejection_code: str = REJ_OK
    message: str = ""
    detail: dict = field(default_factory=dict)
    follow_up_ticket_ids: list[str] = field(default_factory=list)
    scope_label: str = SCOPE_UNKNOWN

    def __bool__(self) -> bool:
        return self.ok


# --- Helpers ----------------------------------------------------------------

def path_in_scope(file_path: str, ticket_scope: set[str]) -> bool:
    """True iff file_path starts with any prefix in ticket_scope.

    ticket_scope is a set of repo-relative path prefixes the ticket owns
    (parsed from the ticket's `components:` frontmatter + AC-implied paths).
    Empty file_path or empty ticket_scope → False (defensive default).

    Prefix-boundary discipline: the character immediately after the matched
    prefix MUST be `/` (path separator) or end-of-string. Without this
    guard, `path_in_scope("services/auditor/foo.go", {"services/audit"})`
    returns True — sibling-directory collision. The trailing `/` on the
    scope entry is also accepted as an equivalent boundary marker.
    """
    if not isinstance(file_path, str) or not file_path:
        return False
    if not ticket_scope:
        return False
    for prefix in ticket_scope:
        # None or non-string prefixes are silently skipped (defensive —
        # an upstream caller may have populated ticket_scope incorrectly).
        if not isinstance(prefix, str) or not prefix:
            continue
        # Strip trailing slash from prefix for consistent matching.
        canonical_prefix = prefix.rstrip("/")
        if not canonical_prefix:
            continue
        if file_path == canonical_prefix:
            return True
        # file_path starts with prefix AND the next char is `/` — boundary match.
        if file_path.startswith(canonical_prefix + "/"):
            return True
    return False


def classify_scope_label(
    failure: dict,
    *,
    ticket_scope: set[str],
) -> str:
    """Classify the scope of a single FAIL verdict.

    Returns one of:
      - `SCOPE_INSIDE`  — every evidence file:line is in ticket_scope.
      - `SCOPE_OUTSIDE` — at least one evidence file:line is OUTSIDE
                          ticket_scope (or follow_up_ticket_ids present
                          with no file evidence).
      - `SCOPE_UNKNOWN`  — empty evidence, no follow-ups, OR declared
                          label contradicts evidence (gaming signal).

    The closed-set return value is one of SCOPE_LABELS. Follow-up
    presence is NOT consulted here — that's `classify_verdict_failure`'s
    job (it differentiates OUTSIDE+follow-ups → PARTIAL from
    OUTSIDE-without-follow-ups → AMBIGUOUS+REJ_FOLLOW_UP_TICKETS_REQUIRED).
    """
    evidence = failure.get("evidence") or []
    followups = failure.get("follow_up_ticket_ids") or []

    # Compute scope from evidence file paths (or follow-ups if no evidence).
    file_paths = [
        ev.get("file") for ev in evidence
        if isinstance(ev, dict) and ev.get("file")
    ]

    # If the verifier declared scope_label, validate it against evidence.
    declared = (failure.get("scope_label") or "").lower()
    if declared in SCOPE_LABELS and file_paths:
        # Self-consistency check — defense against the
        # "mark every FAIL as outside_call_site" escape hatch.
        if declared == SCOPE_OUTSIDE:
            if all(path_in_scope(p, ticket_scope) for p in file_paths):
                # Label says OUTSIDE but every evidence file is INSIDE
                # ticket scope → gaming signal → UNKNOWN.
                return SCOPE_UNKNOWN
        if declared == SCOPE_INSIDE:
            if all(not path_in_scope(p, ticket_scope) for p in file_paths):
                # Inverse gaming: label says INSIDE but evidence is OUTSIDE.
                return SCOPE_UNKNOWN
        return declared

    # No declared label (or no evidence to validate against) — derive.
    if not file_paths:
        if followups:
            # Follow-ups without file:line evidence — trust the follow-ups
            # (the verifier surfaced substrate work via ticket IDs).
            return SCOPE_OUTSIDE
        return SCOPE_UNKNOWN

    inside_count = sum(1 for p in file_paths if path_in_scope(p, ticket_scope))
    outside_count = len(file_paths) - inside_count

    if outside_count > 0:
        return SCOPE_OUTSIDE
    if inside_count > 0:
        return SCOPE_INSIDE
    return SCOPE_UNKNOWN


# --- Classifiers -------------------------------------------------------------

def classify_verdict_failure(
    failure: dict,
    *,
    ticket_scope: set[str],
) -> ResolutionResult:
    """Decide if a single failing lens is substrate-drift or impl-defect.

    The decision is **scope-based**, not confidence-based — the doctrine
    (per Layer 29-31 evidence codified by TKT-NNN) classifies on
    `evidence[].file` scope + `follow_up_ticket_ids` presence, NOT on a
    confidence float. A thorough FAIL with concrete substrate evidence +
    follow-up tickets ALWAYS wins over a narrow PASS, regardless of
    confidence values.

    Args:
        failure: dict with keys:
          - verdict (required, must be "FAIL")
          - evidence (list of {file, line, note}) — verifier's file:line proof
          - follow_up_ticket_ids (list[str], optional) — the substrate work,
            must match canonical `TKT-<PHASE>-<NNN>(-<slug>)?` format
          - scope_label (optional, in SCOPE_LABELS) — verifier's declared scope
        ticket_scope: set of repo-relative path prefixes the ticket owns
          (parsed from the ticket's `components:` frontmatter). MUST be
          non-empty — empty ticket_scope returns AMBIGUOUS.

    Returns:
        ResolutionResult. chosen_resolution is one of:
        - RESOLUTION_PARTIAL_WITH_FOLLOW_UPS (substrate drift + follow-ups)
        - RESOLUTION_LOOP_UNTIL_PASS (implementation defect — in-scope)
        - RESOLUTION_AMBIGUOUS (cannot resolve; orchestrator decides)

    Never raises — the orchestrator decides what to do on AMBIGUOUS.
    """
    if not isinstance(failure, dict):
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_INPUT_NOT_DICT,
            message=f"failure must be a dict, got {type(failure).__name__}",
        )

    verdict = failure.get("verdict")
    if verdict != "FAIL":
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_VERDICT_NOT_FAIL,
            message=f"classify_verdict_failure called on non-FAIL verdict: {verdict!r}",
        )

    # Empty ticket_scope is AMBIGUOUS — without a populated scope, the
    # classifier cannot tell in-scope from out-of-scope and would silently
    # route everything to PARTIAL_WITH_FOLLOW_UPS (Attack #4 from the
    # failure_modes verifier). The worker MUST populate ticket_scope
    # (parsed from the ticket's `components:` frontmatter) before
    # classify_verdict_failure can return a meaningful decision.
    if not ticket_scope:
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_SCOPE_LABEL_UNKNOWN,
            scope_label=SCOPE_UNKNOWN,
            message=(
                "ticket_scope is empty; the orchestrator MUST populate "
                "ticket_scope from the ticket's components: frontmatter "
                "before classify_verdict_failure can resolve a FAIL"
            ),
            detail={"reason": "ticket_scope_empty"},
        )

    followups = failure.get("follow_up_ticket_ids") or []

    # Lightweight format check on follow-up ticket IDs. Malformed IDs
    # would corrupt `tickets-index.md` if forwarded to the cascade update.
    if followups:
        invalid_ids = [tid for tid in followups if not _is_valid_ticket_id(tid)]
        if invalid_ids:
            return ResolutionResult(
                ok=False,
                chosen_resolution=RESOLUTION_AMBIGUOUS,
                rejection_code=REJ_INVALID_FOLLOW_UP_TICKET_ID,
                scope_label=SCOPE_UNKNOWN,
                message=(
                    f"follow_up_ticket_ids contains {len(invalid_ids)} "
                    f"malformed ID(s) ({invalid_ids[:3]}); the ticket "
                    f"factory enforces canonical format — re-file the IDs"
                ),
                detail={"invalid_ids": invalid_ids},
            )

    # Self-consistency check: declared scope_label contradicts the
    # evidence (defense against label gaming — Risk #2 in the design).
    # Done BEFORE classifying, so we can emit the right rejection code.
    declared = (failure.get("scope_label") or "").lower()
    evidence = failure.get("evidence") or []
    file_paths = [
        ev.get("file") for ev in evidence
        if isinstance(ev, dict) and ev.get("file")
    ]
    if declared == SCOPE_OUTSIDE and file_paths and all(
        path_in_scope(p, ticket_scope) for p in file_paths
    ):
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_SCOPE_LABEL_OUTSIDE_BUT_EVIDENCE_INSIDE,
            scope_label=SCOPE_UNKNOWN,
            message=(
                "scope_label says 'outside_ticket_scope' but every evidence "
                "file is INSIDE ticket scope; gaming signal — orchestrator "
                "decision required (cannot trust the label)"
            ),
            detail={"declared_label": declared, "evidence_count": len(file_paths)},
        )
    if declared == SCOPE_INSIDE and file_paths and all(
        not path_in_scope(p, ticket_scope) for p in file_paths
    ):
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_SCOPE_LABEL_UNKNOWN,
            scope_label=SCOPE_UNKNOWN,
            message=(
                "scope_label says 'inside_ticket_scope' but every evidence "
                "file is OUTSIDE ticket scope; orchestrator decision required"
            ),
            detail={"declared_label": declared, "evidence_count": len(file_paths)},
        )

    scope_label = classify_scope_label(failure, ticket_scope=ticket_scope)

    # Case (b) — substrate drift OUTSIDE ticket scope + follow-ups.
    if scope_label == SCOPE_OUTSIDE and followups:
        return ResolutionResult(
            ok=True,
            chosen_resolution=RESOLUTION_PARTIAL_WITH_FOLLOW_UPS,
            scope_label=scope_label,
            message=(
                f"verifier FAIL is substrate drift (scope={scope_label}); "
                f"PARTIAL + {len(followups)} follow-up ticket(s)"
            ),
            detail={"scope_label": scope_label},
            follow_up_ticket_ids=list(followups),
        )

    # Case (a) — implementation defect INSIDE ticket scope.
    if scope_label == SCOPE_INSIDE:
        return ResolutionResult(
            ok=True,
            chosen_resolution=RESOLUTION_LOOP_UNTIL_PASS,
            scope_label=scope_label,
            message=(
                f"verifier FAIL is implementation defect (scope={scope_label}); "
                f"loop until PASS, capped at 3 retries per §Step 7 trigger #4"
            ),
            detail={"scope_label": scope_label},
        )

    # Case (c) — UNKNOWN scope, OR OUTSIDE without follow-ups.
    # Both are AMBIGUOUS — the orchestrator must decide.
    if scope_label == SCOPE_OUTSIDE and not followups:
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_FOLLOW_UP_TICKETS_REQUIRED,
            scope_label=scope_label,
            message=(
                "scope is OUTSIDE but no follow_up_ticket_ids were provided; "
                "the worker must file follow-up tickets before choosing PARTIAL"
            ),
            detail={"scope_label": scope_label},
        )

    # scope_label == SCOPE_UNKNOWN — could not classify.
    return ResolutionResult(
        ok=False,
        chosen_resolution=RESOLUTION_AMBIGUOUS,
        rejection_code=REJ_SCOPE_LABEL_UNKNOWN,
        scope_label=scope_label,
        message=(
            f"verifier FAIL has scope={scope_label} (no evidence file:line "
            f"or contradictory evidence); orchestrator decision required"
        ),
        detail={"scope_label": scope_label},
    )


def arbitrate(
    verdicts: list[dict],
    *,
    ticket_scope: set[str],
) -> ResolutionResult:
    """Resolve n-way conflicting verdicts on the same lens.

    Precedence:
      1. If ANY verdict is FAIL with scope=OUTSIDE + follow_up_ticket_ids,
         the verdict is PARTIAL_WITH_FOLLOW_UPS (thorough FAIL overrides
         narrow PASS — Layer 29 evidence: a FAIL naming 3 P0 substrate gaps
         is more informative than a bare PASS).
      2. If all verdicts agree (all PASS or all FAIL with consistent scope):
         - All PASS → LOOP_UNTIL_PASS is a no-op (the worker is already
           past the loop; the result just records agreement).
         - All FAIL with consistent in-scope scope → LOOP_UNTIL_PASS.
         - All FAIL with consistent outside-scope + follow-ups →
           PARTIAL_WITH_FOLLOW_UPS.
      3. Mixed evidence (some FAIL-no-evidence, some PASS-no-evidence) →
         AMBIGUOUS.

    Args:
        verdicts: list of verdict dicts (each with verdict, evidence,
                  follow_up_ticket_ids, scope_label).
        ticket_scope: set of repo-relative path prefixes the ticket owns.

    Returns:
        ResolutionResult. chosen_resolution is one of:
        - RESOLUTION_PARTIAL_WITH_FOLLOW_UPS
        - RESOLUTION_LOOP_UNTIL_PASS
        - RESOLUTION_AMBIGUOUS
    """
    if not verdicts:
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
            message="no verdicts provided",
        )

    # Bail early if any verdict is malformed — orchestrator decides.
    for v in verdicts:
        if not isinstance(v, dict):
            return ResolutionResult(
                ok=False,
                chosen_resolution=RESOLUTION_AMBIGUOUS,
                rejection_code=REJ_INPUT_NOT_DICT,
                message=f"verdict must be a dict, got {type(v).__name__}",
            )

    # Classify each verdict.
    classified: list[tuple[dict, ResolutionResult]] = []
    for v in verdicts:
        if v.get("verdict") == "FAIL":
            classified.append((v, classify_verdict_failure(
                v, ticket_scope=ticket_scope,
            )))
        else:
            # PASS — mark as loop-no-op.
            classified.append((v, ResolutionResult(
                ok=True,
                chosen_resolution=RESOLUTION_LOOP_UNTIL_PASS,
                scope_label=(v.get("scope_label") or SCOPE_UNKNOWN),
                message="verdict is PASS; loop no-op",
                detail={"verdict": v.get("verdict")},
            )))

    # If any FAIL classified as AMBIGUOUS (not PARTIAL or LOOP), the
    # whole arbitration is AMBIGUOUS — surface to orchestrator.
    has_ambiguous_fail = any(
        not r.ok for _, r in classified
    )
    if has_ambiguous_fail:
        return ResolutionResult(
            ok=False,
            chosen_resolution=RESOLUTION_AMBIGUOUS,
            rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
            message=(
                f"at least one FAIL verdict is AMBIGUOUS "
                f"(scope unclear or missing follow-ups); "
                f"orchestrator decision required"
            ),
            detail={"ambiguous_count": sum(1 for _, r in classified if not r.ok)},
        )

    # Precedence rule 1: any thorough OUTSIDE FAIL with follow-ups wins.
    thorough_partials = [
        (v, r) for v, r in classified
        if r.ok and r.chosen_resolution == RESOLUTION_PARTIAL_WITH_FOLLOW_UPS
    ]
    if thorough_partials:
        # Union the follow-up ticket IDs across all thorough FAILs.
        all_followups: list[str] = []
        for _, r in thorough_partials:
            for tid in r.follow_up_ticket_ids:
                if tid not in all_followups:
                    all_followups.append(tid)
        return ResolutionResult(
            ok=True,
            chosen_resolution=RESOLUTION_PARTIAL_WITH_FOLLOW_UPS,
            message=(
                f"{len(thorough_partials)} thorough FAIL(s) with "
                f"{len(all_followups)} follow-up ticket(s) override "
                f"narrow PASS verdicts (Layer 29 doctrine)"
            ),
            follow_up_ticket_ids=all_followups,
            detail={"thorough_partial_count": len(thorough_partials)},
        )

    # Precedence rule 2: all agree.
    resolutions = {r.chosen_resolution for _, r in classified}
    if resolutions == {RESOLUTION_LOOP_UNTIL_PASS}:
        return ResolutionResult(
            ok=True,
            chosen_resolution=RESOLUTION_LOOP_UNTIL_PASS,
            message=f"all {len(classified)} verdicts agree → LOOP_UNTIL_PASS",
        )
    if len(resolutions) == 1:
        # All same non-LOOP resolution (shouldn't happen given the
        # exhaustive set, but defensive).
        return ResolutionResult(
            ok=True,
            chosen_resolution=resolutions.pop(),
            message=f"all {len(classified)} verdicts agree",
        )

    # Precedence rule 3: mixed → AMBIGUOUS.
    return ResolutionResult(
        ok=False,
        chosen_resolution=RESOLUTION_AMBIGUOUS,
        rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
        message=(
            f"verdicts disagree ({len(resolutions)} distinct resolutions); "
            f"orchestrator decision required"
        ),
        detail={"resolutions": sorted(resolutions)},
    )


def resolve_verdict_conflict(
    first: dict,
    second: dict,
    *,
    ticket_scope: set[str],
) -> ResolutionResult:
    """2-way convenience wrapper around `arbitrate`.

    Provided for backward-compat with the TKT-NNN AC #3 test name
    "conflicting verdicts". New code should call `arbitrate(verdicts, ...)`
    directly to support n-way conflicts.
    """
    return arbitrate([first, second], ticket_scope=ticket_scope)


# --- Schema validator -------------------------------------------------------

def validate_verdict_conflict_field(verdict_conflict: object) -> ResolutionResult:
    """Schema-validate the WORKER_RESULT.verification.verdict_conflict object.

    Required keys:
      - first_verdict:        "PASS" | "FAIL"
      - chosen_resolution:    "LOOP_UNTIL_PASS" | "PARTIAL_WITH_FOLLOW_UPS" | "AMBIGUOUS"

    Optional keys:
      - second_verdict:       "PASS" | "FAIL"
      - scope_difference_note: str
      - rationale:            str
    """
    if verdict_conflict is None:
        return ResolutionResult(
            ok=True,
            message="verdict_conflict is None/absent — optional field, treated as valid",
        )
    if not isinstance(verdict_conflict, dict):
        return ResolutionResult(
            ok=False,
            rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
            message=f"verdict_conflict must be a dict, got {type(verdict_conflict).__name__}",
        )

    required = {
        "first_verdict": ("PASS", "FAIL"),
        "chosen_resolution": (
            RESOLUTION_LOOP_UNTIL_PASS,
            RESOLUTION_PARTIAL_WITH_FOLLOW_UPS,
            RESOLUTION_AMBIGUOUS,
        ),
    }
    optional = {
        "second_verdict": ("PASS", "FAIL"),
        "scope_difference_note": (str,),
        "rationale": (str,),
    }

    for key, allowed in required.items():
        if key not in verdict_conflict:
            return ResolutionResult(
                ok=False,
                rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
                message=f"verdict_conflict missing required key: {key!r}",
                detail={"missing": key},
            )
        val = verdict_conflict[key]
        if val not in allowed:
            return ResolutionResult(
                ok=False,
                rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
                message=f"verdict_conflict[{key!r}]={val!r} not in {sorted(allowed)}",
                detail={"key": key, "value": val, "allowed": sorted(allowed)},
            )

    for key, type_spec in optional.items():
        if key not in verdict_conflict:
            continue
        val = verdict_conflict[key]
        if val is None:
            continue
        # type_spec is either a tuple of types or a tuple of allowed
        # values (strings). Treat it as a value-set if all elements are
        # strings/instances of the same non-type category; otherwise as
        # a type spec for isinstance.
        if type_spec and isinstance(type_spec[0], type):
            # Tuple of types — use isinstance.
            if not isinstance(val, type_spec):
                return ResolutionResult(
                    ok=False,
                    rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
                    message=f"verdict_conflict[{key!r}]={val!r} not of type {type_spec}",
                    detail={"key": key, "value": val, "expected_type": str(type_spec)},
                )
        else:
            # Tuple of allowed values — use membership.
            if val not in type_spec:
                return ResolutionResult(
                    ok=False,
                    rejection_code=REJ_VERDICT_CONFLICT_MALFORMED,
                    message=f"verdict_conflict[{key!r}]={val!r} not in {sorted(type_spec)}",
                    detail={"key": key, "value": val, "allowed": sorted(type_spec)},
                )

    return ResolutionResult(
        ok=True,
        message="verdict_conflict field is valid",
    )


def build_verdict_conflict_record(
    first: dict,
    second: dict,
    resolution: ResolutionResult,
) -> dict:
    """Build the canonical `verdict_conflict` field for WORKER_RESULT.verification.

    Mirrors the schema in CONVENTIONS.md §Sub-agent return schemas.
    """
    return {
        "first_verdict": first.get("verdict"),
        "second_verdict": second.get("verdict"),
        "scope_difference_note": (
            f"first.scope={_safe_scope(first)}; "
            f"second.scope={_safe_scope(second)}"
        ),
        "chosen_resolution": resolution.chosen_resolution,
        "rationale": resolution.message,
    }


def _safe_scope(verdict: dict) -> str:
    """Return the scope label of a verdict, or 'unknown' if not a dict."""
    if not isinstance(verdict, dict):
        return "unknown"
    scope = (verdict.get("scope_label") or "unknown").lower()
    if scope in SCOPE_LABELS:
        return scope
    return "unknown"


# --- Self-check (smoke) -----------------------------------------------------

if __name__ == "__main__":
    import sys

    # Layer 29 case (TKT-NNNb).
    layer29_failure = {
        "verdict": "FAIL",
        "evidence": [
            {"file": "db/migrations/V001__init.sql", "line": 1, "note": "audit_outbox table not created"},
            {"file": "services/the project's outbox-drainer/src/queries.py", "line": 7, "note": "no audit_outbox drainer"},
            {"file": "the project's API service/cmd/main.go", "line": 159, "note": "idempotency middleware not mounted"},
        ],
        "follow_up_ticket_ids": [
            "TKT-NNNb-FOUND-001",
            "TKT-NNNb-FOUND-002",
            "TKT-NNNb-FOUND-003",
        ],
    }
    ticket_scope = {"the project's API service/internal/audit/"}
    r = classify_verdict_failure(layer29_failure, ticket_scope=ticket_scope)
    print("Layer 29 case:", r.ok, r.chosen_resolution, r.message)
    print("  follow_up_ticket_ids:", r.follow_up_ticket_ids)
    if not r.ok or r.chosen_resolution != RESOLUTION_PARTIAL_WITH_FOLLOW_UPS:
        print("FAIL: expected PARTIAL_WITH_FOLLOW_UPS")
        sys.exit(1)

    # Impl defect case (in-scope evidence).
    impl_defect = {
        "verdict": "FAIL",
        "evidence": [
            {"file": "the project's outbox-drainer service", "line": 117,
             "note": "the project's outbox-append helper swallows ErrNoTenantID"},
        ],
        # no follow_up_ticket_ids — call-site defect.
    }
    ticket_scope2 = {"the project's API service/internal/audit/"}
    r2 = classify_verdict_failure(impl_defect, ticket_scope=ticket_scope2)
    print("Impl defect case:", r2.ok, r2.chosen_resolution, r2.message)
    if not r2.ok or r2.chosen_resolution != RESOLUTION_LOOP_UNTIL_PASS:
        print("FAIL: expected LOOP_UNTIL_PASS")
        sys.exit(1)

    # Arbitrate 2-way.
    fail_thorough = {
        "verdict": "FAIL",
        "evidence": [{"file": "db/migrations/V001__init.sql", "line": 1, "note": "substrate"}],
        "follow_up_ticket_ids": ["TKT-FOUND-001"],
    }
    pass_narrow = {"verdict": "PASS", "evidence": []}
    r3 = arbitrate([fail_thorough, pass_narrow], ticket_scope=ticket_scope)
    print("Arbitrate (thorough FAIL + narrow PASS):", r3.ok, r3.chosen_resolution)
    if not r3.ok or r3.chosen_resolution != RESOLUTION_PARTIAL_WITH_FOLLOW_UPS:
        print("FAIL: expected PARTIAL_WITH_FOLLOW_UPS")
        sys.exit(1)

    sys.exit(0)
