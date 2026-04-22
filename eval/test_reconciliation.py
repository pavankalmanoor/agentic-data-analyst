"""Reconciliation tests (Layer 5).

Two halves:

A. Adversarial — fixture-based cases (no LLM, no DB). Each case feeds
   a hand-rolled plan + list of fake sibling results into reconcile()
   with ``skip_llm=True`` and asserts the deterministic severity /
   passed / shape come out right. This is the rule-coverage layer.

B. Happy path — runs the real pipeline (planner -> generator ->
   reconcile) on the 5 Layer-3 questions and asserts:
     - single-path questions skip cleanly (skipped=True)
     - cross-val questions reconcile to severity="low"

Run: python -m eval.test_reconciliation
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

import pandas as pd

from agents.query_planner import load_data_dictionary, plan_query
from agents.sql_generator import GenerationResult, generate_and_execute
from scrutiny.reconciliation import (
    ReconciliationResult,
    estimate_cost,
    reconcile,
    symmetric_delta,
)


# ---------------------------------------------------------------------------
# Test fixture helpers
# ---------------------------------------------------------------------------
def _mk_plan(
    reconciliation_step: str | None,
    sub_question_specs: list[tuple[int, str | None]],
) -> SimpleNamespace:
    """Build a minimal stand-in for a QueryPlan.

    ``sub_question_specs`` is a list of (id, canonical_metric) tuples.
    """
    sub_questions = [
        SimpleNamespace(id=sqid, canonical_metric=cm)
        for sqid, cm in sub_question_specs
    ]
    return SimpleNamespace(
        restated_question="(fixture) test question",
        reconciliation_step=reconciliation_step,
        sub_questions=sub_questions,
    )


def _mk_result(
    sub_question_id: int,
    dataframe: pd.DataFrame,
    notes: str = "(fixture)",
) -> SimpleNamespace:
    """Duck-typed GenerationResult for fixtures."""
    return SimpleNamespace(
        sub_question_id=sub_question_id,
        dataframe=dataframe,
        notes=notes,
        row_count=len(dataframe),
        sql="",
        sql_raw="",
        elapsed_ms=0,
        limit_injected=False,
        usage={},
    )


# ---------------------------------------------------------------------------
# Half A — adversarial / rule-coverage
# ---------------------------------------------------------------------------
@dataclass
class ReconCase:
    name: str
    plan: SimpleNamespace
    results: list
    expectation: Callable[[ReconciliationResult], bool]


CASES: list[ReconCase] = [
    # ---- Skip paths ----
    ReconCase(
        name="no reconciliation_step, single sub-question -> skip (single_sub_question)",
        plan=_mk_plan(None, [(1, "unique_customers")]),
        results=[_mk_result(1, pd.DataFrame({"unique_customers": [96096]}))],
        expectation=lambda r: (
            r.skipped and r.passed and r.severity == "low"
            and r.shape == "skipped"
            and r.skip_category == "single_sub_question"
        ),
    ),
    ReconCase(
        name="no reconciliation_step, multi sub-question -> skip (complementary_views)",
        plan=_mk_plan(None, [(1, None), (2, None)]),
        results=[
            _mk_result(1, pd.DataFrame({"bucket": ["a"], "avg": [4.5]})),
            _mk_result(2, pd.DataFrame({"bucket": ["b"], "avg": [3.2]})),
        ],
        expectation=lambda r: (
            r.skipped and r.passed and r.skip_category == "complementary_views"
        ),
    ),
    ReconCase(
        name="reconciliation_step set but only one sibling -> skip (insufficient_siblings)",
        plan=_mk_plan("reconcile two defs", [(1, "revenue_from_payments")]),
        results=[_mk_result(1, pd.DataFrame({"revenue": [2_053_000.0]}))],
        expectation=lambda r: (
            r.skipped and r.passed
            and r.skip_category == "insufficient_siblings"
        ),
    ),

    # ---- Scalar band coverage ----
    ReconCase(
        name="scalar agreement within 2% -> low",
        plan=_mk_plan(
            "reconcile revenue defs",
            [(1, "revenue_from_items_and_freight"),
             (2, "revenue_from_payments")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({"revenue": [2_053_421.42]})),
            _mk_result(2, pd.DataFrame({"revenue": [2_042_118.55]})),
        ],
        expectation=lambda r: (
            not r.skipped and r.passed
            and r.severity == "low"
            and r.shape == "scalar"
            and r.delta_pct is not None and r.delta_pct < 0.02
        ),
    ),
    ReconCase(
        name="scalar 2-5% delta -> medium",
        plan=_mk_plan(
            "reconcile revenue defs",
            [(1, "revenue_from_items_and_freight"),
             (2, "revenue_from_payments")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({"revenue": [2_000_000.0]})),
            _mk_result(2, pd.DataFrame({"revenue": [2_080_000.0]})),
        ],
        expectation=lambda r: (
            not r.skipped and r.passed and r.severity == "medium"
            and r.delta_pct is not None
            and 0.02 < r.delta_pct <= 0.05
        ),
    ),
    ReconCase(
        name="scalar >5% delta -> high (blocked)",
        plan=_mk_plan(
            "reconcile revenue defs",
            [(1, "revenue_from_items_and_freight"),
             (2, "revenue_from_payments")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({"revenue": [2_000_000.0]})),
            _mk_result(2, pd.DataFrame({"revenue": [14_000_000.0]})),
        ],
        expectation=lambda r: (
            not r.skipped and not r.passed
            and r.severity == "high"
            and r.delta_pct is not None and r.delta_pct > 0.05
        ),
    ),

    # ---- Multi-row paths ----
    ReconCase(
        name="multi-row aligned keys within 2% -> low",
        plan=_mk_plan(
            "reconcile quarterly revenue defs",
            [(1, "revenue_from_items_and_freight"),
             (2, "revenue_from_payments")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({
                "quarter": ["2017Q1", "2017Q2", "2017Q3", "2017Q4"],
                "revenue": [1_000_000, 1_500_000, 2_000_000, 2_500_000],
            })),
            _mk_result(2, pd.DataFrame({
                "quarter": ["2017Q1", "2017Q2", "2017Q3", "2017Q4"],
                "revenue": [1_005_000, 1_510_000, 2_010_000, 2_520_000],
            })),
        ],
        expectation=lambda r: (
            r.shape == "multi_row" and r.key_alignment == "ok"
            and r.severity == "low" and r.passed
            and r.max_delta_pct is not None and r.max_delta_pct < 0.02
        ),
    ),
    ReconCase(
        name="multi-row key mismatch -> high (blocked)",
        plan=_mk_plan(
            "reconcile quarterly revenue defs",
            [(1, "revenue_from_items_and_freight"),
             (2, "revenue_from_payments")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({
                "quarter": ["2017Q1", "2017Q2", "2017Q3", "2017Q4"],
                "revenue": [1.0, 2.0, 3.0, 4.0],
            })),
            _mk_result(2, pd.DataFrame({
                "quarter": ["2017Q1", "2017Q3", "2017Q4"],
                "revenue": [1.0, 3.0, 4.0],
            })),
        ],
        expectation=lambda r: (
            r.shape == "multi_row" and r.key_alignment == "failed"
            and r.severity == "high" and not r.passed
        ),
    ),
    ReconCase(
        name="multi-row different key columns -> high (blocked)",
        plan=_mk_plan(
            "reconcile quarterly revenue defs",
            [(1, "revenue_a"), (2, "revenue_b")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({
                "quarter": ["2017Q1", "2017Q2"],
                "revenue": [1.0, 2.0],
            })),
            _mk_result(2, pd.DataFrame({
                "month": ["2017-01", "2017-02"],
                "revenue": [1.0, 2.0],
            })),
        ],
        expectation=lambda r: (
            r.severity == "high" and not r.passed
            and r.key_alignment == "failed"
        ),
    ),

    # ---- Inference / shape failures ----
    ReconCase(
        name="sibling with 2 numeric cols -> high (ambiguous inference)",
        plan=_mk_plan(
            "reconcile two defs",
            [(1, "a"), (2, "b")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({"x": [1.0], "y": [2.0]})),
            _mk_result(2, pd.DataFrame({"revenue": [2.0]})),
        ],
        expectation=lambda r: (
            r.severity == "high" and not r.passed
            and r.shape == "invalid"
        ),
    ),
    ReconCase(
        name="multi-row siblings share one numeric name, extra aid col ignored -> low",
        # Mirrors the Q12 failure mode: the SQL generator emits an
        # incidental ``order_count`` alongside the canonical
        # ``total_revenue`` in one sibling. Cross-sibling
        # disambiguation should pick ``total_revenue`` as the metric
        # everywhere and reconcile cleanly.
        plan=_mk_plan(
            "reconcile revenue by payment type",
            [(1, "revenue_from_items_and_freight"),
             (2, "revenue_from_payments")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({
                "payment_type": ["credit_card", "boleto"],
                "total_revenue": [1000.0, 500.0],
                "order_count": [10, 5],
            })),
            _mk_result(2, pd.DataFrame({
                "payment_type": ["credit_card", "boleto"],
                "total_revenue": [1000.0, 500.0],
            })),
        ],
        expectation=lambda r: (
            r.severity == "low" and r.passed
            and r.shape == "multi_row"
            and r.key_alignment == "ok"
        ),
    ),
    ReconCase(
        name="multi-row siblings with 2 numeric cols and no shared name -> high",
        # Both siblings are ambiguous AND there's no overlap in
        # numeric column names — cross-sibling disambiguation can't
        # rescue us, so we still bail loudly.
        plan=_mk_plan(
            "reconcile two defs",
            [(1, "a"), (2, "b")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({
                "payment_type": ["credit_card"],
                "revenue_a": [1000.0],
                "order_count": [10],
            })),
            _mk_result(2, pd.DataFrame({
                "payment_type": ["credit_card"],
                "revenue_b": [1000.0],
                "avg_value": [100.0],
            })),
        ],
        expectation=lambda r: (
            r.severity == "high" and not r.passed
            and r.shape == "invalid"
        ),
    ),
    ReconCase(
        name="shape mismatch (scalar vs multi-row) -> high",
        plan=_mk_plan(
            "reconcile two defs",
            [(1, "a"), (2, "b")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({"revenue": [5_000_000.0]})),
            _mk_result(2, pd.DataFrame({
                "quarter": ["Q1", "Q2"],
                "revenue": [2_000_000, 3_000_000],
            })),
        ],
        expectation=lambda r: (
            r.severity == "high" and not r.passed
            and r.shape == "invalid"
        ),
    ),

    # ---- Edge case: both zero ----
    ReconCase(
        name="both-zero scalars -> low (match)",
        plan=_mk_plan(
            "reconcile two defs",
            [(1, "a"), (2, "b")],
        ),
        results=[
            _mk_result(1, pd.DataFrame({"revenue": [0.0]})),
            _mk_result(2, pd.DataFrame({"revenue": [0.0]})),
        ],
        expectation=lambda r: (
            r.severity == "low" and r.passed
            and r.delta_pct == 0.0
        ),
    ),
]


def run_adversarial() -> tuple[int, int, list[str]]:
    passed = failed = 0
    errors: list[str] = []
    for i, case in enumerate(CASES, start=1):
        result = reconcile(case.plan, case.results, skip_llm=True)
        ok = bool(case.expectation(result))
        mark = "PASS" if ok else "FAIL"
        print(f"[Rule {i:2d}/{len(CASES)}] {mark}  {case.name}")
        print(f"         -> skipped={result.skipped} passed={result.passed} "
              f"severity={result.severity} shape={result.shape} "
              f"delta_pct={result.delta_pct} key_alignment={result.key_alignment}")
        if result.reason:
            print(f"         reason: {result.reason}")
        if ok:
            passed += 1
        else:
            failed += 1
            errors.append(case.name)
    return passed, failed, errors


# ---------------------------------------------------------------------------
# Half B — happy-path through the real pipeline
# ---------------------------------------------------------------------------
@dataclass
class HappyCase:
    name: str
    question: str
    expectation: Callable[[ReconciliationResult], bool]


HAPPY_CASES: list[HappyCase] = [
    HappyCase(
        name="Q3 2017 revenue (cross-val)",
        question="What was Q3 2017 revenue?",
        expectation=lambda r: (
            not r.skipped and r.passed
            and r.severity in ("low", "medium")
            and r.shape == "scalar"
        ),
    ),
    HappyCase(
        name="Unique customers (single-path, expect skip)",
        question="How many unique customers placed orders?",
        expectation=lambda r: r.skipped and r.passed,
    ),
    HappyCase(
        name="Complaints by category (single-path, expect skip)",
        question="Which product category has the most complaints?",
        expectation=lambda r: r.skipped and r.passed,
    ),
    HappyCase(
        name="Booked revenue by quarter 2017 (multi-row cross-val)",
        question=(
            "What was booked revenue by quarter in 2017, and how does it "
            "reconcile across different definitions?"
        ),
        expectation=lambda r: (
            not r.skipped and r.passed
            and r.severity in ("low", "medium")
            and r.shape == "multi_row"
            and r.key_alignment == "ok"
        ),
    ),
    HappyCase(
        name="Worst on-time sellers (single-path, expect skip)",
        question=(
            "Which sellers have the worst on-time delivery performance "
            "among sellers with at least 100 delivered orders?"
        ),
        expectation=lambda r: r.skipped and r.passed,
    ),
]


def run_happy_path() -> tuple[int, int, list[str], dict[str, int]]:
    dictionary = load_data_dictionary()
    passed = failed = 0
    errors: list[str] = []
    total_usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }

    for i, case in enumerate(HAPPY_CASES, start=1):
        print("-" * 78)
        print(f"[Happy {i}/{len(HAPPY_CASES)}] {case.name}")
        print(f"  Q: {case.question}")
        plan, _ = plan_query(case.question, dictionary)
        if not plan.answerable:
            print("  Planner refused — skipping")
            failed += 1
            errors.append(f"{case.name}: planner refused")
            continue

        results: list[GenerationResult] = []
        try:
            for sq in plan.sub_questions:
                r = generate_and_execute(sq.model_dump(), dictionary)
                results.append(r)
        except Exception as e:
            print(f"  GENERATOR RAISED: {type(e).__name__}: {e}")
            failed += 1
            errors.append(f"{case.name}: generator raised")
            continue

        recon = reconcile(plan, results)
        for k in total_usage:
            total_usage[k] += recon.usage.get(k, 0)

        print(f"    -> skipped={recon.skipped} passed={recon.passed} "
              f"severity={recon.severity} shape={recon.shape} "
              f"delta_pct={recon.delta_pct} "
              f"key_alignment={recon.key_alignment}")
        if recon.reason:
            print(f"       reason: {recon.reason}")
        if recon.note:
            print(f"       note  : {recon.note}")

        ok = bool(case.expectation(recon))
        mark = "PASS" if ok else "FAIL"
        print(f"    {mark}")
        if ok:
            passed += 1
        else:
            failed += 1
            errors.append(case.name)

    return passed, failed, errors, total_usage


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> int:
    t0 = time.time()

    print("=" * 78)
    print("Symmetric delta unit check")
    print("=" * 78)
    assert symmetric_delta(100, 100) == 0.0
    assert symmetric_delta(0, 0) == 0.0
    assert abs(symmetric_delta(100, 104) - (4 / 102.0)) < 1e-9
    assert symmetric_delta(100, 0) == 2.0  # |100-0| / (50) = 2.0
    print("  ok")

    print()
    print("=" * 78)
    print("Half A — Adversarial rule coverage (no LLM)")
    print("=" * 78)
    rule_pass, rule_fail, rule_errors = run_adversarial()

    print()
    print("=" * 78)
    print("Half B — Happy-path reconciliation (real pipeline + LLM note)")
    print("=" * 78)
    happy_pass, happy_fail, happy_errors, usage = run_happy_path()

    t_wall = time.time() - t0
    total_pass = rule_pass + happy_pass
    total_fail = rule_fail + happy_fail

    print()
    print("=" * 78)
    print(f"Rules       : {rule_pass} passed, {rule_fail} failed, "
          f"of {len(CASES)}")
    print(f"Happy-path  : {happy_pass} passed, {happy_fail} failed, "
          f"of {len(HAPPY_CASES)}")
    print(f"TOTAL       : {total_pass} passed, {total_fail} failed")
    print()
    print(f"Wall time                : {t_wall:.1f}s")
    print(f"Input tokens (recon LLM) : {usage['input_tokens']:,}")
    print(f"  cache write            : {usage['cache_creation_input_tokens']:,}")
    print(f"  cache read             : {usage['cache_read_input_tokens']:,}")
    print(f"Output tokens            : {usage['output_tokens']:,}")
    print(f"Est. reconciliation cost : ${estimate_cost(usage):.4f}")
    print()
    print("Note: happy-path also incurs planner + generator costs, which")
    print("are tracked in their own evals, not here.")

    if rule_errors or happy_errors:
        print()
        print("Failures:")
        for e in rule_errors + happy_errors:
            print(f"  - {e}")

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
