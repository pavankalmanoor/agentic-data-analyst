"""Confidence scoring — Layer 6.

Deterministic, pure-Python derivation of the confidence label from the
outcomes of the earlier pipeline layers. No LLM calls. The label's
placement on the presenter report is narrative only — the decision
itself lives here so it's reproducible and unit-testable.

Label semantics (FOUNDATION §2.5)
---------------------------------
- **UNABLE**: planner refused, OR any sub-question exhausted its retry
  budget with sanity still failing.
- **LOW**: sanity flagged any sub-question at high severity (blocked,
  but retries remained so we continued), OR reconciliation mismatched
  beyond tolerance, OR any sub-question had retry_count > 0 (first
  attempt was wrong enough to fail sanity; conservative even if the
  retry then passed).
- **MEDIUM**: any caveat short of a hard block — sanity medium, recon
  medium, recon skipped. Not HIGH because there's something worth
  surfacing; not LOW because nothing hard-failed.
- **HIGH**: sanity clean on every sub-question AND reconciliation
  passed at severity="low" (or reconciliation was applicable and
  cleanly matched). Reconciliation-skipped cases do NOT earn HIGH for
  now — see the TODO below.

Current conservative choice: reconciliation-skipped == MEDIUM, even
when the question is genuinely single-path (e.g., unique customers).
Rationale: the pipeline today cannot distinguish "not applicable" from
"planner oversight." Layer 7 should add an explicit planner annotation
to split these paths; that TODO lives on task #39.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


ConfidenceLabel = Literal["HIGH", "MEDIUM", "LOW", "UNABLE"]


@dataclass
class ConfidenceResult:
    label: ConfidenceLabel
    reason: str
    # Structured derivation for eval / debugging. Not surfaced to the
    # presenter — the presenter uses `reason` and the individual layer
    # results directly.
    derivation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "reason": self.reason,
            "derivation": self.derivation,
        }


# ---------------------------------------------------------------------------
# Duck-typed accessors
# ---------------------------------------------------------------------------
# We accept any object exposing the right attributes so fixtures and test
# stubs don't have to construct real SanityResult / ReconciliationResult
# objects. The canonical shapes are in scrutiny.sanity and
# scrutiny.reconciliation.


def _sanity_severity(sanity: Any) -> str:
    """Return one of {'none','low','medium','high'} from a SanityResult."""
    return getattr(sanity, "severity", "none")


def _sanity_passed(sanity: Any) -> bool:
    return bool(getattr(sanity, "passed", True))


def _recon_skipped(recon: Any) -> bool:
    if recon is None:
        return True
    return bool(getattr(recon, "skipped", True))


def _recon_passed(recon: Any) -> bool:
    if recon is None:
        return True
    return bool(getattr(recon, "passed", True))


def _recon_severity(recon: Any) -> str:
    if recon is None:
        return "low"
    return getattr(recon, "severity", "low")


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------
def derive_confidence(
    plan: Any,
    sub_outcomes: Iterable[dict[str, Any]],
    reconciliation: Any,
    *,
    max_retries: int = 2,
) -> ConfidenceResult:
    """Compute the pipeline-wide confidence label.

    Parameters
    ----------
    plan:
        The planner's QueryPlan (or any duck-typed stand-in exposing
        ``answerable`` and ``unanswerable_reason``).
    sub_outcomes:
        One dict per sub-question executed. Expected keys:
          - ``sanity``: a SanityResult-like object
          - ``retry_count``: int
          - ``retries_exhausted``: bool  (True iff retry_count == max_retries
             AND sanity still failing on the final attempt)
          - ``sub_question_id``: int (optional, used in the derivation trail)
    reconciliation:
        A ReconciliationResult or None. None is treated as skipped.
    max_retries:
        The retry ceiling the orchestrator used. Currently informational
        (the ``retries_exhausted`` flag in each sub_outcome is what
        matters), but kept for the derivation record.

    Returns
    -------
    ConfidenceResult
        ``label`` ∈ {HIGH, MEDIUM, LOW, UNABLE} plus a short ``reason``
        and a structured ``derivation`` dict for debugging.
    """
    outcomes = list(sub_outcomes)
    derivation: dict[str, Any] = {
        "planner_answerable": bool(getattr(plan, "answerable", True)),
        "max_retries": max_retries,
        "sub_count": len(outcomes),
        "sanity_severities": [
            _sanity_severity(o.get("sanity")) for o in outcomes
        ],
        "retry_counts": [int(o.get("retry_count", 0)) for o in outcomes],
        "retries_exhausted_any": any(
            bool(o.get("retries_exhausted", False)) for o in outcomes
        ),
        "recon_skipped": _recon_skipped(reconciliation),
        "recon_passed": _recon_passed(reconciliation),
        "recon_severity": _recon_severity(reconciliation),
    }

    # -- UNABLE paths ---------------------------------------------------
    if not derivation["planner_answerable"]:
        reason = (
            f"Planner declined: "
            f"{getattr(plan, 'unanswerable_reason', 'no reason given')}"
        )
        return ConfidenceResult(
            label="UNABLE", reason=reason, derivation=derivation,
        )

    if derivation["retries_exhausted_any"]:
        failing_ids = [
            o.get("sub_question_id", "?")
            for o in outcomes
            if o.get("retries_exhausted", False)
        ]
        reason = (
            f"Sub-question(s) {failing_ids} exhausted the retry budget "
            f"({max_retries}) with sanity still failing; declining "
            f"rather than returning an unreliable number."
        )
        return ConfidenceResult(
            label="UNABLE", reason=reason, derivation=derivation,
        )

    # -- LOW paths ------------------------------------------------------
    # Any high-severity sanity still in the outcomes = hard sanity fail.
    # (If retries had resolved it, severity wouldn't be "high" on the
    # final recorded outcome. Keep this check distinct from
    # retries_exhausted for the derivation trail.)
    any_sanity_high = any(
        _sanity_severity(o.get("sanity")) == "high" for o in outcomes
    )
    if any_sanity_high:
        flagged = [
            o.get("sub_question_id", "?")
            for o in outcomes
            if _sanity_severity(o.get("sanity")) == "high"
        ]
        reason = (
            f"Sanity flagged high severity on sub-question(s) {flagged} "
            f"and the retry budget has not been exhausted; marking LOW "
            f"so downstream consumers know the numbers are not trusted."
        )
        return ConfidenceResult(
            label="LOW", reason=reason, derivation=derivation,
        )

    # Reconciliation hard mismatch (severity=high, not passed) but retry
    # budget available. Under the current design we do NOT auto-retry on
    # reconciliation failure (see BUILD_PLAN Layer 5 discussion), so this
    # lands as LOW, not UNABLE.
    if (
        not derivation["recon_skipped"]
        and not derivation["recon_passed"]
    ):
        reason = (
            f"Reconciliation mismatch at severity=high "
            f"(delta={getattr(reconciliation, 'delta_pct', None)}); "
            f"per design we do not auto-retry on reconciliation failure, "
            f"so the answer is surfaced with LOW confidence."
        )
        return ConfidenceResult(
            label="LOW", reason=reason, derivation=derivation,
        )

    # Any successful retry means the first attempt failed sanity. The
    # retry may have recovered cleanly, but the pipeline demonstrated
    # fragility. Conservative default: LOW. (FOUNDATION §2.5 LOW case:
    # "sanity flagged ... within retry budget.")
    any_retry = any(int(o.get("retry_count", 0)) > 0 for o in outcomes)
    if any_retry:
        retried_ids = [
            o.get("sub_question_id", "?")
            for o in outcomes
            if int(o.get("retry_count", 0)) > 0
        ]
        reason = (
            f"Sub-question(s) {retried_ids} required a retry to pass "
            f"sanity; the first-attempt SQL was wrong enough to fail "
            f"deterministic checks. Final value shown but confidence "
            f"is LOW so reviewers know to look at the SQL."
        )
        return ConfidenceResult(
            label="LOW", reason=reason, derivation=derivation,
        )

    # -- MEDIUM paths ---------------------------------------------------
    # Reconciliation skipped: conservative MEDIUM per FOUNDATION §2.5.
    # See task #39 for the Layer 7 upgrade that would promote genuine
    # single-path questions to HIGH.
    if derivation["recon_skipped"]:
        reason = (
            "Reconciliation was not applicable (single-path question); "
            "per current policy this is MEDIUM even when sanity is "
            "clean, pending a planner annotation to distinguish "
            "genuinely-single-path from should-have-been-cross-val."
        )
        return ConfidenceResult(
            label="MEDIUM", reason=reason, derivation=derivation,
        )

    # Reconciliation passed but at medium severity (loose tolerance).
    if (
        _recon_severity(reconciliation) == "medium"
        and _recon_passed(reconciliation)
    ):
        reason = (
            f"Reconciliation passed at severity=medium "
            f"(delta={getattr(reconciliation, 'delta_pct', None)}); "
            f"surface the caveat but do not block."
        )
        return ConfidenceResult(
            label="MEDIUM", reason=reason, derivation=derivation,
        )

    # Any medium-severity sanity flags (null_rate_high, all_zero, etc.).
    if any(
        _sanity_severity(o.get("sanity")) == "medium" for o in outcomes
    ):
        flagged = [
            o.get("sub_question_id", "?")
            for o in outcomes
            if _sanity_severity(o.get("sanity")) == "medium"
        ]
        reason = (
            f"Sanity flagged medium-severity issue(s) on sub-question(s) "
            f"{flagged} (e.g., high null rate or all-zero column); "
            f"pipeline did not block but a caveat is warranted."
        )
        return ConfidenceResult(
            label="MEDIUM", reason=reason, derivation=derivation,
        )

    # -- HIGH -----------------------------------------------------------
    reason = (
        "Sanity clean on every sub-question and reconciliation passed "
        "at severity=low; no retries were required."
    )
    return ConfidenceResult(
        label="HIGH", reason=reason, derivation=derivation,
    )
