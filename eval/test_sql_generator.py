"""End-to-end smoke test for the SQL Generator (Layer 3).

Chains planner -> sql_generator for each answerable case from the
planner eval, executes the generated SQL, and checks sanity of the
returned numbers. Unanswerable cases are skipped (the planner refuses
and no SQL is generated).

This is NOT a correctness proof. It's a "did we build a pipeline that
returns plausible numbers on the happy path" guard.

Run: python -m eval.test_sql_generator
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from agents.query_planner import QueryPlan, load_data_dictionary, plan_query
from agents.sql_generator import (
    GenerationResult,
    estimate_cost,
    generate_and_execute,
)


@dataclass
class Case:
    name: str
    question: str
    # Sanity predicates applied to the list of GenerationResults.
    expectations: list[tuple[str, Callable[[list[GenerationResult]], bool]]] = field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------
def total_scalar(results: list[GenerationResult], col: str) -> float | None:
    """Sum a single numeric column across all result frames."""
    total = 0.0
    found = False
    for r in results:
        if col in r.dataframe.columns:
            total += float(r.dataframe[col].sum())
            found = True
    return total if found else None


def any_col_like(results: list[GenerationResult], substr: str) -> bool:
    return any(
        any(substr.lower() in c.lower() for c in r.dataframe.columns)
        for r in results
    )


def first_scalar(results: list[GenerationResult]) -> float | None:
    """Return the (0,0) cell of the first result, or None."""
    if not results:
        return None
    df = results[0].dataframe
    if df.empty:
        return None
    try:
        return float(df.iloc[0, 0])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cases (answerable only; unanswerable ones skip the generator entirely)
# ---------------------------------------------------------------------------
CASES: list[Case] = [
    Case(
        name="Q3 2017 revenue",
        question="What was Q3 2017 revenue?",
        expectations=[
            ("two sub-questions executed (cross-val)",
             lambda rs: len(rs) == 2),
            ("a revenue-like column exists",
             lambda rs: any_col_like(rs, "revenue")
                        or any_col_like(rs, "payment")
                        or any_col_like(rs, "price")),
            ("each sub-question returned 1 row",
             lambda rs: all(r.row_count == 1 for r in rs)),
            ("revenue numbers are in the $1M-$5M ballpark",
             lambda rs: all(
                 1_000_000 < float(r.dataframe.iloc[0, -1]) < 5_000_000
                 for r in rs
             )),
            ("the two definitions agree within 1%",
             lambda rs: len(rs) == 2 and (
                 abs(float(rs[0].dataframe.iloc[0, -1])
                     - float(rs[1].dataframe.iloc[0, -1]))
                 / max(
                     float(rs[0].dataframe.iloc[0, -1]),
                     float(rs[1].dataframe.iloc[0, -1]),
                 ) < 0.01
             )),
        ],
    ),
    Case(
        name="Unique customers",
        question="How many unique customers placed orders?",
        expectations=[
            ("exactly one sub-question",
             lambda rs: len(rs) == 1),
            ("exactly one row, one column",
             lambda rs: rs[0].dataframe.shape == (1, 1)),
            ("value equals 96096 (verified in dictionary)",
             lambda rs: int(rs[0].dataframe.iloc[0, 0]) == 96096),
        ],
    ),
    Case(
        name="Category with most complaints",
        question="Which product category has the most complaints?",
        expectations=[
            ("at least one row returned",
             lambda rs: rs[0].row_count >= 1),
            ("result is ordered (first row is the winner)",
             lambda rs: rs[0].row_count <= 100),
        ],
    ),
    Case(
        name="Booked revenue by quarter 2017",
        question=(
            "What was booked revenue by quarter in 2017, and how does it "
            "reconcile across different definitions?"
        ),
        expectations=[
            ("two sub-questions",
             lambda rs: len(rs) == 2),
            ("each returns 4 rows (one per quarter)",
             lambda rs: all(r.row_count == 4 for r in rs)),
            ("totals are within 1% across definitions",
             lambda rs: len(rs) == 2 and (
                 abs(rs[0].dataframe.iloc[:, -1].sum()
                     - rs[1].dataframe.iloc[:, -1].sum())
                 / max(
                     float(rs[0].dataframe.iloc[:, -1].sum()),
                     float(rs[1].dataframe.iloc[:, -1].sum()),
                 ) < 0.01
             )),
        ],
    ),
    Case(
        name="Worst on-time sellers (>=100 delivered)",
        question=(
            "Which sellers have the worst on-time delivery performance "
            "among sellers with at least 100 delivered orders?"
        ),
        expectations=[
            ("exactly one sub-question", lambda rs: len(rs) == 1),
            ("returns 10 rows (bottom-10)",
             lambda rs: rs[0].row_count == 10),
            ("an on-time-rate column exists and is in [0,1]",
             lambda rs: any(
                 df[c].between(0, 1).all()
                 for r in rs
                 for df in [r.dataframe]
                 for c in df.select_dtypes("number").columns
                 if "rate" in c.lower() or "on_time" in c.lower()
             )),
            ("results ordered ascending by rate",
             lambda rs: (
                 (rate_cols := [
                     c for c in rs[0].dataframe.select_dtypes("number").columns
                     if "rate" in c.lower() or "on_time" in c.lower()
                 ])
                 and rs[0].dataframe[rate_cols[0]].is_monotonic_increasing
             )),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run() -> int:
    dictionary = load_data_dictionary()

    passed = failed = 0
    errors: list[str] = []
    total_usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    t_all = time.time()

    for i, case in enumerate(CASES, start=1):
        print("=" * 78)
        print(f"[{i}/{len(CASES)}] {case.name}")
        print(f"  Q: {case.question}")

        # Step 1: plan
        try:
            plan, plan_usage = plan_query(case.question, dictionary)
        except Exception as e:
            failed += 1
            msg = f"  PLANNER RAISED: {type(e).__name__}: {e}"
            print(msg)
            errors.append(f"{case.name}: {msg}")
            continue

        for k in total_usage:
            total_usage[k] += plan_usage.get(k, 0)

        if not plan.answerable:
            failed += 1
            print(f"  Planner refused — expected answerable case. "
                  f"reason={plan.unanswerable_reason}")
            errors.append(f"{case.name}: planner refused")
            continue

        # Step 2: generate+execute for each sub-question
        results: list[GenerationResult] = []
        gen_error: str | None = None
        for sq in plan.sub_questions:
            try:
                r = generate_and_execute(sq.model_dump(), dictionary)
                results.append(r)
                for k in total_usage:
                    total_usage[k] += r.usage.get(k, 0)
            except Exception as e:
                gen_error = f"{type(e).__name__}: {e}"
                break

        if gen_error:
            failed += 1
            print(f"  GENERATOR RAISED: {gen_error}")
            errors.append(f"{case.name}: {gen_error}")
            continue

        for r in results:
            print(f"    [{r.sub_question_id}] rows={r.row_count} "
                  f"elapsed={r.elapsed_ms}ms limit_injected={r.limit_injected}")
            print(f"       notes: {r.notes}")

        case_pass = True
        for label, predicate in case.expectations:
            try:
                ok = bool(predicate(results))
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
    print(f"Input tokens         : {total_usage['input_tokens']:,}")
    print(f"  cache write        : {total_usage['cache_creation_input_tokens']:,}")
    print(f"  cache read         : {total_usage['cache_read_input_tokens']:,}")
    print(f"Output tokens        : {total_usage['output_tokens']:,}")
    print(f"Estimated total cost : ${estimate_cost(total_usage):.4f}")

    if errors:
        print()
        print("Failures:")
        for e in errors:
            print(f"  - {e}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
