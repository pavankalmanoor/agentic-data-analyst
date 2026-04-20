"""Smoke test for the Query Planner (Layer 2).

Not a pytest suite — a runnable script that exercises the planner on a
mix of answerable and unanswerable questions and prints a compact
pass/fail report. Expectations are intentionally loose: we assert the
things that must be true (answerable flag, cross-val flag when a
multi-definition metric is implicated), not the exact sub-question text.

Run: python -m eval.test_planner
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from agents.query_planner import (
    QueryPlan,
    estimate_cost,
    load_data_dictionary,
    plan_query,
)


@dataclass
class Case:
    name: str
    question: str
    # Each expectation: (label, predicate(plan) -> bool).
    expectations: list[tuple[str, Callable[[QueryPlan], bool]]] = field(
        default_factory=list
    )


def _has_metric(plan: QueryPlan, metric: str) -> bool:
    return any(sq.canonical_metric == metric for sq in plan.sub_questions)


def _any_cross_val(plan: QueryPlan) -> bool:
    return any(sq.cross_validation_candidate for sq in plan.sub_questions)


CASES: list[Case] = [
    # --- 5 from the build plan ------------------------------------------
    Case(
        name="Q3 2017 revenue (should cross-validate)",
        question="What was Q3 2017 revenue?",
        expectations=[
            ("answerable=true", lambda p: p.answerable),
            ("uses revenue metric", lambda p: _has_metric(p, "revenue")),
            ("flags cross-validation", _any_cross_val),
            (
                "reconciliation_step is set",
                lambda p: bool(p.reconciliation_step),
            ),
        ],
    ),
    Case(
        name="Unique customers (simple, single sub-question)",
        question="How many unique customers placed orders?",
        expectations=[
            ("answerable=true", lambda p: p.answerable),
            ("uses unique_customers metric",
             lambda p: _has_metric(p, "unique_customers")),
            (
                "not flagged cross-val (single definition)",
                lambda p: not _any_cross_val(p),
            ),
            ("exactly one sub-question", lambda p: len(p.sub_questions) == 1),
        ],
    ),
    Case(
        name="CAC (unanswerable — no cost data)",
        question="What's our customer acquisition cost?",
        expectations=[
            ("answerable=false", lambda p: not p.answerable),
            ("reason cites cost/CAC",
             lambda p: bool(p.unanswerable_reason)
                       and any(w in p.unanswerable_reason.lower()
                               for w in ("cost", "cac", "marketing"))),
            ("no consolation sub-questions", lambda p: len(p.sub_questions) == 0),
        ],
    ),
    Case(
        name="Category with most complaints",
        question="Which product category has the most complaints?",
        # We accept either: (a) answered via low review scores, or
        # (b) refused because 'complaints' isn't a distinct field.
        # What we don't accept is a plan that silently pretends the
        # dataset has a complaints column.
        expectations=[
            (
                "no sub-question mentions a nonexistent 'complaints' column",
                lambda p: not any(
                    "complaint" in " ".join(sq.filters_implied).lower()
                    and "review" not in " ".join(sq.filters_implied).lower()
                    for sq in p.sub_questions
                ),
            ),
        ],
    ),
    Case(
        name="Next-quarter revenue (unanswerable — future)",
        question="What will revenue be next quarter?",
        expectations=[
            ("answerable=false", lambda p: not p.answerable),
            ("reason cites time coverage / future / 2018",
             lambda p: bool(p.unanswerable_reason) and any(
                 w in p.unanswerable_reason.lower()
                 for w in ("time", "coverage", "future", "2018", "forecast")
             )),
        ],
    ),
    # --- 3 adversarial questions from the user --------------------------
    Case(
        name="Booked revenue by quarter 2017 with reconciliation",
        question=(
            "What was booked revenue by quarter in 2017, and how does it "
            "reconcile across different definitions?"
        ),
        expectations=[
            ("answerable=true", lambda p: p.answerable),
            ("uses revenue metric", lambda p: _has_metric(p, "revenue")),
            ("at least two sub-questions (for cross-check)",
             lambda p: len(p.sub_questions) >= 2),
            ("flags cross-validation", _any_cross_val),
            ("reconciliation_step is set",
             lambda p: bool(p.reconciliation_step)),
        ],
    ),
    Case(
        name="Worst on-time sellers with >=100 delivered orders",
        question=(
            "Which sellers have the worst on-time delivery performance "
            "among sellers with at least 100 delivered orders?"
        ),
        expectations=[
            ("answerable=true", lambda p: p.answerable),
            ("uses on_time_delivery_rate metric",
             lambda p: _has_metric(p, "on_time_delivery_rate")),
            (
                "not flagged cross-val (single definition)",
                lambda p: not _any_cross_val(p),
            ),
            (
                "HAVING or >=100 surfaced in filters",
                lambda p: any(
                    "100" in f or "having" in f.lower()
                    for sq in p.sub_questions
                    for f in sq.filters_implied
                ),
            ),
        ],
    ),
    Case(
        name="Profit margin Q1 2018 by category (unanswerable)",
        question="What was our profit margin in Q1 2018 by category?",
        expectations=[
            ("answerable=false", lambda p: not p.answerable),
            ("reason cites margin/cost/profit",
             lambda p: bool(p.unanswerable_reason)
                       and any(w in p.unanswerable_reason.lower()
                               for w in ("margin", "cost", "profit", "cogs"))),
            ("no consolation (no revenue sub-question)",
             lambda p: len(p.sub_questions) == 0),
        ],
    ),
]


def run() -> int:
    dictionary = load_data_dictionary()

    total_usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    passed = failed = 0
    errors: list[str] = []

    t_all = time.time()
    for i, case in enumerate(CASES, start=1):
        print("=" * 78)
        print(f"[{i}/{len(CASES)}] {case.name}")
        print(f"  Q: {case.question}")
        t0 = time.time()
        try:
            plan, usage = plan_query(case.question, dictionary)
        except Exception as e:
            failed += 1
            msg = f"  RAISED: {type(e).__name__}: {e}"
            print(msg)
            errors.append(f"{case.name}: {msg}")
            continue
        elapsed = time.time() - t0

        for k in total_usage:
            total_usage[k] += usage.get(k, 0)

        print(f"  Restated: {plan.restated_question}")
        print(f"  Answerable: {plan.answerable}  "
              f"sub_qs: {len(plan.sub_questions)}  "
              f"reconciliation: {'yes' if plan.reconciliation_step else 'no'}  "
              f"({elapsed:.1f}s)")

        case_pass = True
        for label, predicate in case.expectations:
            try:
                ok = bool(predicate(plan))
            except Exception as e:
                ok = False
                label = f"{label} [predicate raised: {e}]"
            mark = "PASS" if ok else "FAIL"
            print(f"    {mark}  {label}")
            if not ok:
                case_pass = False
                errors.append(f"{case.name}: {label}")

        if case_pass:
            passed += 1
        else:
            failed += 1
    t_wall = time.time() - t_all

    print("=" * 78)
    print(f"Summary: {passed} passed, {failed} failed, out of {len(CASES)}")
    print()
    print(f"Wall time            : {t_wall:.1f}s")
    print(f"Input tokens (sum)   : {total_usage['input_tokens']:,}")
    print(f"  cache write        : {total_usage['cache_creation_input_tokens']:,}")
    print(f"  cache read         : {total_usage['cache_read_input_tokens']:,}")
    print(f"Output tokens (sum)  : {total_usage['output_tokens']:,}")
    print(f"Estimated total cost : ${estimate_cost(total_usage):.4f}")

    if errors:
        print()
        print("Failures:")
        for e in errors:
            print(f"  - {e}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
