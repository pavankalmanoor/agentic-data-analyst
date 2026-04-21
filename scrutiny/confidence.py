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

Skip taxonomy (task #39 — Layer 7)
----------------------------------
Reconciliation can be skipped for three distinct reasons, surfaced by
``ReconciliationResult.skip_category``:

- ``single_sub_question`` — plan has exactly one sub-question. HIGH is
  eligible only when the sub-question is either backed by a documented
  ``canonical_metric`` OR flagged ``aggregation_heavy=False`` (simple
  row counts / lookups). Bespoke aggregation-heavy single paths stay
  MEDIUM because silent composition errors are most likely there and
  there is no second path to cross-check.
- ``complementary_views`` — plan has >=2 sub-questions but the planner
  intentionally left ``reconciliation_step`` null (different bucketings
  of one population, e.g. "how does X relate to Y"). Complementary
  views are not cross-validation; stay MEDIUM.
- ``insufficient_siblings`` — planner wanted reconciliation but one of
  the sibling sub-questions didn't produce a result. Stay MEDIUM.
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


def _recon_skip_category(recon: Any) -> str | None:
    """Return the structured skip category emitted by Layer 5, or None.

    ``None`` if recon wasn't skipped. For older ReconciliationResult
    instances that predate task #39 (no ``skip_category`` attribute),
    defaults to ``"single_sub_question"`` so they continue to flow
    through the same conservative MEDIUM path.
    """
    if recon is None:
        return None
    if not bool(getattr(recon, "skipped", False)):
        return None
    return getattr(recon, "skip_category", None)


def _single_path_high_eligible(plan: Any) -> bool:
    """True iff the plan's single sub-question is a good HIGH candidate
    when reconciliation skipped cleanly.

    Heuristic (task #39): the single sub-question must be either
      (a) backed by a documented ``canonical_metric`` (the planner
          matched a well-defined business metric with exactly one
          definition in the dictionary, so there is nothing to
          reconcile against — the skip is a legitimate no-op); OR
      (b) flagged ``aggregation_heavy=False`` by the planner (row
          counts or simple lookups — no AVG/SUM/rate fragility).

    Plans that are both ``canonical_metric=None`` AND
    ``aggregation_heavy=True`` stay at MEDIUM: bespoke derivations with
    aggregation (bucketing, derived delay_days, multi-hop joins with
    AVG/STDDEV) are where silent composition errors are most likely,
    and without a second path we have no way to catch them.
    """
    subs = list(getattr(plan, "sub_questions", []) or [])
    if len(subs) != 1:
        return False
    sq = subs[0]
    if getattr(sq, "canonical_metric", None) is not None:
        return True
    if not bool(getattr(sq, "aggregation_heavy", True)):
        return True
    return False


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
        "recon_skip_category": _recon_skip_category(reconciliation),
        "single_path_high_eligible": _single_path_high_eligible(plan),
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
    # Sanity medium beats any recon-based branch: an all-zero column or
    # high null rate warrants a caveat even if recon was a clean skip.
    sanity_medium_ids = [
        o.get("sub_question_id", "?")
        for o in outcomes
        if _sanity_severity(o.get("sanity")) == "medium"
    ]
    if sanity_medium_ids:
        reason = (
            f"Sanity flagged medium-severity issue(s) on sub-question(s) "
            f"{sanity_medium_ids} (e.g., high null rate or all-zero "
            f"column); pipeline did not block but a caveat is warranted."
        )
        return ConfidenceResult(
            label="MEDIUM", reason=reason, derivation=derivation,
        )

    # Reconciliation skipped: promote to HIGH only when the skip was a
    # legitimate no-op on a documented canonical metric (task #39).
    # Complementary-view plans and bespoke single-sub-question plans
    # stay at MEDIUM because there's no second path to cross-check
    # and the pipeline can't rule out a silent composition error.
    if derivation["recon_skipped"]:
        skip_cat = derivation["recon_skip_category"]
        if (
            skip_cat == "single_sub_question"
            and derivation["single_path_high_eligible"]
        ):
            reason = (
                "Single-path question — either backed by a documented "
                "canonical metric or a simple (non-aggregation-heavy) "
                "lookup. Sanity clean and no retries, so the skipped "
                "reconciliation is a legitimate no-op and confidence "
                "is HIGH."
            )
            return ConfidenceResult(
                label="HIGH", reason=reason, derivation=derivation,
            )
        if skip_cat == "complementary_views":
            reason = (
                "Planner emitted multiple complementary-view "
                "sub-questions (different bucketings of one population); "
                "there is no second path to cross-check, so confidence "
                "caps at MEDIUM."
            )
        elif skip_cat == "insufficient_siblings":
            reason = (
                "Planner requested reconciliation but fewer than 2 "
                "sibling results arrived; unable to cross-check, so "
                "confidence caps at MEDIUM."
            )
        else:  # single_sub_question without canonical_metric, or unknown
            reason = (
                "Single-path question without a documented canonical "
                "metric (bespoke SQL composition); no second path to "
                "cross-check, so confidence caps at MEDIUM."
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

    # -- HIGH -----------------------------------------------------------
    reason = (
        "Sanity clean on every sub-question and reconciliation passed "
        "at severity=low; no retries were required."
    )
    return ConfidenceResult(
        label="HIGH", reason=reason, derivation=derivation,
    )
