"""End-to-end pipeline tests (Layer 6 integration).

Runs the full ``main.run_pipeline`` on curated questions and asserts
pipeline-level invariants. This is NOT a point-value correctness
test — individual layers have their own evals for that. Here we check
that the layers compose correctly:

  1. Q3 2017 revenue (cross-val):
       - planner answerable
       - at least one sub-question executed
       - reconciliation ran (not skipped) and passed
       - confidence is not UNABLE
       - presenter emits all four non-empty sections with the
         literal confidence label in ``answer``

  2. Customer Acquisition Cost (planner refusal):
       - plan.answerable == False
       - no sub-questions executed
       - reconciliation is None (short-circuited)
       - confidence.label == UNABLE
       - metrics.{generator_usages, sanity_usages} are empty
       - presenter emits all four sections and cites the
         unanswerable reason

A third case for retry-path recovery is intentionally deferred. We
cannot force a retry deterministically (it depends on the generator
emitting bad SQL on the first attempt), and a flaky test is worse
than a missing one. If we later find an input that reliably induces
a first-pass sanity fail on a clean DB, add it here.

Run: python -m eval.test_end_to_end
"""
from __future__ import annotations

import sys
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from agents.presenter import estimate_cost as estimate_presenter_cost
from agents.query_planner import estimate_cost as estimate_planner_cost
from agents.sql_generator import estimate_cost as estimate_generator_cost
from scrutiny.reconciliation import estimate_cost as estimate_reconciliation_cost
from scrutiny.sanity import estimate_cost as estimate_sanity_cost

from main import RunMetrics, run_pipeline


# ---------------------------------------------------------------------------
# Case definition
# ---------------------------------------------------------------------------
@dataclass
class E2ECase:
    name: str
    question: str
    # Returns (ok, [assertion messages]). Runs after the pipeline.
    assertion: Callable[..., tuple[bool, list[str]]]


def _fmt_usage(usage: dict) -> str:
    return (
        f"in={usage.get('input_tokens', 0):,} "
        f"cwr={usage.get('cache_creation_input_tokens', 0):,} "
        f"crd={usage.get('cache_read_input_tokens', 0):,} "
        f"out={usage.get('output_tokens', 0):,}"
    )


# ---------------------------------------------------------------------------
# Case 1 — Q3 2017 revenue (full cross-val path)
# ---------------------------------------------------------------------------
def _assert_q3_revenue(plan, outcomes, reconciliation, confidence, presentation, metrics):
    """Happy-path cross-val assertions.

    Tolerant on the final severity/label: drift may land HIGH or MEDIUM
    depending on recon delta and generator nondeterminism. The shape
    invariants (answerable, recon ran, confidence ≠ UNABLE, 4 sections)
    are the hard asserts.
    """
    msgs: list[str] = []
    ok = True

    if not plan.answerable:
        ok = False
        msgs.append(f"planner refused: {plan.unanswerable_reason!r}")

    if len(outcomes) < 1:
        ok = False
        msgs.append(f"expected >=1 sub-question outcome, got {len(outcomes)}")

    if reconciliation is None:
        ok = False
        msgs.append("reconciliation was None (expected a result object)")
    else:
        if reconciliation.skipped:
            ok = False
            msgs.append(
                f"reconciliation unexpectedly skipped: {reconciliation.reason!r}"
            )
        if not reconciliation.passed:
            ok = False
            msgs.append(
                f"reconciliation did not pass: severity={reconciliation.severity} "
                f"delta_pct={reconciliation.delta_pct}"
            )

    if confidence.label == "UNABLE":
        ok = False
        msgs.append(f"confidence=UNABLE for a happy-path question: {confidence.reason!r}")

    # Presenter: four non-empty sections; literal confidence label must appear
    # in the answer section (the UI parses this token).
    out = presentation.output
    for section in ("answer", "methodology", "verification", "caveats"):
        if not getattr(out, section, "").strip():
            ok = False
            msgs.append(f"presenter section {section!r} is empty")
    if confidence.label not in out.answer:
        ok = False
        msgs.append(
            f"confidence label {confidence.label!r} not found literally in "
            f"answer section"
        )

    # Sanity-of-the-pipeline: every sub-question should have been executed
    # (we have sql + row_count), not skipped or stubbed.
    for o in outcomes:
        if not o.generation.sql.strip():
            ok = False
            msgs.append(f"sub_question {o.sub_question.id}: empty SQL recorded")

    return ok, msgs


# ---------------------------------------------------------------------------
# Case 2 — CAC refusal (short-circuit)
# ---------------------------------------------------------------------------
def _assert_cac_refusal(plan, outcomes, reconciliation, confidence, presentation, metrics):
    msgs: list[str] = []
    ok = True

    if plan.answerable:
        ok = False
        msgs.append("expected planner to refuse; it returned answerable=True")
    if not (plan.unanswerable_reason or "").strip():
        ok = False
        msgs.append("planner refused but unanswerable_reason is empty")

    if outcomes:
        ok = False
        msgs.append(
            f"expected 0 sub-question outcomes on refusal, got {len(outcomes)}"
        )

    if reconciliation is not None:
        ok = False
        msgs.append(
            f"expected reconciliation=None on refusal, got "
            f"skipped={reconciliation.skipped} severity={reconciliation.severity}"
        )

    if confidence.label != "UNABLE":
        ok = False
        msgs.append(
            f"expected confidence.label=UNABLE, got {confidence.label!r} "
            f"({confidence.reason!r})"
        )

    # The whole point of the short-circuit: no generator or sanity calls.
    if metrics.generator_usages:
        ok = False
        msgs.append(
            f"expected no generator usage on refusal, got "
            f"{len(metrics.generator_usages)} call(s)"
        )
    if metrics.sanity_usages:
        ok = False
        msgs.append(
            f"expected no sanity usage on refusal, got "
            f"{len(metrics.sanity_usages)} call(s)"
        )
    if metrics.reconciliation_usage:
        ok = False
        msgs.append(
            "expected no reconciliation usage on refusal, got "
            f"{metrics.reconciliation_usage!r}"
        )

    # Presenter sections still required (UNABLE is allowed short caveats,
    # but not empty ones).
    out = presentation.output
    for section in ("answer", "methodology", "verification", "caveats"):
        if not getattr(out, section, "").strip():
            ok = False
            msgs.append(f"presenter section {section!r} is empty on UNABLE path")

    if "UNABLE" not in out.answer:
        ok = False
        msgs.append("UNABLE label not cited literally in answer section")

    # The presenter must reference the reason — either the planner's
    # unanswerable_reason or the derived confidence reason. We don't
    # demand a verbatim match (the prompt allows paraphrasing around the
    # citation), but at least one distinctive keyword should appear.
    reason_blob = (plan.unanswerable_reason or "") + " " + (confidence.reason or "")
    keywords = ("marketing", "spend", "advertising", "acquisition", "cost")
    if not any(k.lower() in out.answer.lower() for k in keywords):
        ok = False
        msgs.append(
            f"presenter answer does not cite the refusal reason (no keyword "
            f"from {keywords} present). Reason was: {reason_blob!r}"
        )

    return ok, msgs


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
CASES: list[E2ECase] = [
    E2ECase(
        name="Q3 2017 revenue (cross-val, happy path)",
        question="What was Q3 2017 revenue?",
        assertion=_assert_q3_revenue,
    ),
    E2ECase(
        name="Customer acquisition cost (planner refusal)",
        question="What's our customer acquisition cost?",
        assertion=_assert_cac_refusal,
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _accumulate(totals: RunMetrics, run: RunMetrics) -> None:
    """Accumulate per-run usage into a running totals RunMetrics.

    Usage dicts are additive (sum input_tokens, output_tokens, etc.);
    generator/sanity are lists we extend.
    """
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        totals.planner_usage[key] = (
            totals.planner_usage.get(key, 0)
            + run.planner_usage.get(key, 0)
        )
        totals.reconciliation_usage[key] = (
            totals.reconciliation_usage.get(key, 0)
            + run.reconciliation_usage.get(key, 0)
        )
        totals.presenter_usage[key] = (
            totals.presenter_usage.get(key, 0)
            + run.presenter_usage.get(key, 0)
        )
    totals.generator_usages.extend(run.generator_usages)
    totals.sanity_usages.extend(run.sanity_usages)


def _total_cost_parts(m: RunMetrics) -> dict[str, float]:
    return {
        "planner": estimate_planner_cost(m.planner_usage) if m.planner_usage else 0.0,
        "sql_generator": sum(estimate_generator_cost(u) for u in m.generator_usages),
        "sanity": sum(estimate_sanity_cost(u) for u in m.sanity_usages),
        "reconciliation": (
            estimate_reconciliation_cost(m.reconciliation_usage)
            if m.reconciliation_usage else 0.0
        ),
        "presenter": (
            estimate_presenter_cost(m.presenter_usage)
            if m.presenter_usage else 0.0
        ),
    }


def run_cases() -> int:
    totals = RunMetrics()
    n_pass = n_fail = 0
    errors: list[str] = []
    t0 = time.time()

    for i, case in enumerate(CASES, start=1):
        print("=" * 78)
        print(f"[Case {i}/{len(CASES)}] {case.name}")
        print(f"  Q: {case.question}")
        print("-" * 78)

        try:
            plan, outcomes, recon, confidence, presentation, metrics = run_pipeline(
                case.question, verbose=False,
            )
        except Exception as e:
            n_fail += 1
            err = f"{case.name}: pipeline raised {type(e).__name__}: {e}"
            errors.append(err)
            print(f"  PIPELINE RAISED: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

        # Per-run stats before assertions so we see them on failure too.
        print(
            f"  plan.answerable       : {plan.answerable} "
            f"({len(plan.sub_questions)} sub-question(s))"
        )
        if not plan.answerable:
            print(f"  unanswerable_reason   : {plan.unanswerable_reason}")
        print(f"  outcomes executed     : {len(outcomes)}")
        for o in outcomes:
            print(
                f"    - sq{o.sub_question.id}: sanity.passed={o.sanity.passed} "
                f"severity={o.sanity.severity} retries={o.retry_count} "
                f"rows={o.generation.row_count}"
            )
        if recon is None:
            print("  reconciliation        : None (short-circuited)")
        else:
            print(
                f"  reconciliation        : skipped={recon.skipped} "
                f"passed={recon.passed} severity={recon.severity} "
                f"delta_pct={recon.delta_pct}"
            )
        print(f"  confidence            : {confidence.label}")
        print(f"  confidence.reason     : {confidence.reason}")

        # Cost roll-up for this run.
        parts = _total_cost_parts(metrics)
        print(
            "  cost (USD)            : "
            + ", ".join(f"{k}=${v:.4f}" for k, v in parts.items())
            + f", total=${sum(parts.values()):.4f}"
        )

        # Short presenter excerpt.
        answer_one_line = presentation.output.answer.replace("\n", " ")
        if len(answer_one_line) > 200:
            answer_one_line = answer_one_line[:197] + "..."
        print(f"  answer (excerpt)      : {answer_one_line}")

        # Assertions.
        ok, msgs = case.assertion(
            plan, outcomes, recon, confidence, presentation, metrics,
        )
        if ok:
            n_pass += 1
            print("  RESULT                : PASS")
        else:
            n_fail += 1
            errors.append(case.name)
            print("  RESULT                : FAIL")
            for m in msgs:
                print(f"    - {m}")

        _accumulate(totals, metrics)

    wall = time.time() - t0

    # Totals
    print()
    print("=" * 78)
    print(
        f"E2E        : {n_pass} passed, {n_fail} failed, of {len(CASES)}  "
        f"(wall {wall:.1f}s)"
    )
    parts = _total_cost_parts(totals)
    print("Per-layer cost across all cases:")
    for k, v in parts.items():
        print(f"  {k:<15s} ${v:.4f}")
    print(f"  {'TOTAL':<15s} ${sum(parts.values()):.4f}")
    print("LLM call counts:")
    print(f"  planner        : {1 if totals.planner_usage else 0} per case")
    print(f"  sql_generator  : {len(totals.generator_usages)} (all cases)")
    print(f"  sanity         : {len(totals.sanity_usages)} (all cases)")
    print(f"  reconciliation : {1 if totals.reconciliation_usage else 0} (aggregated)")
    print(f"  presenter      : {1 if totals.presenter_usage else 0} (aggregated)")

    if errors:
        print()
        print("Failures:")
        for e in errors:
            print(f"  - {e}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(run_cases())
