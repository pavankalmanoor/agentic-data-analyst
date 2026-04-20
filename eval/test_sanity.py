"""Sanity-check tests (Layer 4).

Two halves:

A. Happy path — feed the sanity checker real results from the planner
   + generator on the 5 answerable questions. Every result should pass
   (passed=True, severity in {none, low}), because we know from Layer 3
   these numbers are good.

B. Adversarial — inject synthetic defects (negative revenue, out-of-range
   rate, empty DataFrame, high null rate) and confirm the rule layer
   flags each one at severity="high" (or medium for null_rate).

The adversarial half runs without any LLM calls — it's pure rule
coverage — so it's fast and free.

Run: python -m eval.test_sanity
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from agents.query_planner import load_data_dictionary, plan_query
from agents.sql_generator import GenerationResult, generate_and_execute
from scrutiny.sanity import (
    SanityResult,
    check_result,
    estimate_cost,
    run_rule_checks,
)


# ---------------------------------------------------------------------------
# Half A — happy path (costs a few cents, exercises LLM)
# ---------------------------------------------------------------------------
HAPPY_QUESTIONS = [
    "What was Q3 2017 revenue?",
    "How many unique customers placed orders?",
    "Which product category has the most complaints?",
    (
        "What was booked revenue by quarter in 2017, and how does it "
        "reconcile across different definitions?"
    ),
    (
        "Which sellers have the worst on-time delivery performance "
        "among sellers with at least 100 delivered orders?"
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

    for i, q in enumerate(HAPPY_QUESTIONS, start=1):
        print("-" * 78)
        print(f"[Happy {i}/{len(HAPPY_QUESTIONS)}] {q}")
        plan, _ = plan_query(q, dictionary)
        if not plan.answerable:
            print("  Planner refused unexpectedly; skipping.")
            continue

        for sq in plan.sub_questions:
            result: GenerationResult = generate_and_execute(sq.model_dump(), dictionary)
            sanity: SanityResult = check_result(
                sq.model_dump(), result.dataframe, result.notes
            )
            for k in total_usage:
                total_usage[k] += sanity.usage.get(k, 0)

            mark = "PASS" if sanity.passed else "FAIL"
            print(f"  [{sq.id}] sanity={mark} severity={sanity.severity} "
                  f"rule_flags={len(sanity.flags)} "
                  f"llm_severity={sanity.llm_severity}")
            if sanity.llm_verdict:
                print(f"      verdict: {sanity.llm_verdict}")
            for flag in sanity.flags:
                print(f"      flag[{flag.severity}] {flag.rule} "
                      f"{flag.column or ''}: {flag.message}")
            for c in sanity.llm_concerns:
                print(f"      concern: {c}")

            if sanity.passed:
                passed += 1
            else:
                failed += 1
                errors.append(f"Happy '{q[:40]}' sub#{sq.id}: "
                              f"severity={sanity.severity}")

    return passed, failed, errors, total_usage


# ---------------------------------------------------------------------------
# Half B — adversarial rule coverage (no LLM)
# ---------------------------------------------------------------------------
@dataclass
class RuleCase:
    name: str
    sub_question: dict
    dataframe: pd.DataFrame
    # Predicate on list[SanityFlag] — must return True for the case to pass.
    expectation: Callable[[list], bool]


RULE_CASES = [
    RuleCase(
        name="negative revenue flagged high",
        sub_question={"id": 1, "question": "revenue",
                      "canonical_metric": "revenue",
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": True,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({"revenue": [-100.0]}),
        expectation=lambda flags: any(
            f.rule == "negative_monetary" and f.severity == "high"
            for f in flags
        ),
    ),
    RuleCase(
        name="rate > 1 flagged high",
        sub_question={"id": 1, "question": "on-time rate",
                      "canonical_metric": "on_time_delivery_rate",
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": True,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({"on_time_rate": [0.4, 1.2, 0.6]}),
        expectation=lambda flags: any(
            f.rule == "rate_out_of_range" and f.severity == "high"
            for f in flags
        ),
    ),
    RuleCase(
        name="percent > 100 flagged high",
        sub_question={"id": 1, "question": "pct",
                      "canonical_metric": None,
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": False,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({"pct_delivered": [45.0, 120.0]}),
        expectation=lambda flags: any(
            f.rule == "percent_out_of_range" and f.severity == "high"
            for f in flags
        ),
    ),
    RuleCase(
        name="empty result flagged high",
        sub_question={"id": 1, "question": "revenue",
                      "canonical_metric": "revenue",
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": True,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({"revenue": []}),
        expectation=lambda flags: any(
            f.rule == "empty_result" and f.severity == "high"
            for f in flags
        ),
    ),
    RuleCase(
        name="high null rate flagged medium",
        sub_question={"id": 1, "question": "avg delivery days",
                      "canonical_metric": "avg_delivery_days",
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": True,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({
            "seller_id": ["a", "b", "c", "d"],
            "avg_delivery_days": [np.nan, np.nan, np.nan, 12.3],
        }),
        expectation=lambda flags: any(
            f.rule == "null_rate_high" and f.severity == "medium"
            for f in flags
        ),
    ),
    RuleCase(
        name="all-zero numeric flagged medium",
        sub_question={"id": 1, "question": "revenue",
                      "canonical_metric": "revenue",
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": True,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({
            "quarter": ["Q1", "Q2", "Q3", "Q4"],
            "revenue": [0.0, 0.0, 0.0, 0.0],
        }),
        expectation=lambda flags: any(
            f.rule == "all_zero_numeric" and f.severity == "medium"
            for f in flags
        ),
    ),
    RuleCase(
        name="clean result yields no rule flags",
        sub_question={"id": 1, "question": "revenue",
                      "canonical_metric": "revenue",
                      "tables_likely": [], "filters_implied": [],
                      "aggregation_heavy": True,
                      "cross_validation_candidate": False},
        dataframe=pd.DataFrame({"revenue": [2_053_421.42]}),
        expectation=lambda flags: len(flags) == 0,
    ),
]


def run_adversarial() -> tuple[int, int, list[str]]:
    passed = failed = 0
    errors: list[str] = []
    for i, case in enumerate(RULE_CASES, start=1):
        flags = run_rule_checks(case.dataframe, case.sub_question)
        ok = case.expectation(flags)
        mark = "PASS" if ok else "FAIL"
        print(f"[Rule {i}/{len(RULE_CASES)}] {mark}  {case.name}  "
              f"(flags={[f.rule for f in flags]})")
        if ok:
            passed += 1
        else:
            failed += 1
            errors.append(case.name)
    return passed, failed, errors


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> int:
    t0 = time.time()

    print("=" * 78)
    print("Half B — Adversarial rule coverage (no LLM)")
    print("=" * 78)
    rule_pass, rule_fail, rule_errors = run_adversarial()

    print()
    print("=" * 78)
    print("Half A — Happy-path sanity checks (LLM calls)")
    print("=" * 78)
    happy_pass, happy_fail, happy_errors, usage = run_happy_path()

    t_wall = time.time() - t0
    total_pass = rule_pass + happy_pass
    total_fail = rule_fail + happy_fail

    print()
    print("=" * 78)
    print(f"Rules       : {rule_pass} passed, {rule_fail} failed, "
          f"of {len(RULE_CASES)}")
    print(f"Happy-path  : {happy_pass} passed, {happy_fail} failed")
    print(f"TOTAL       : {total_pass} passed, {total_fail} failed")
    print()
    print(f"Wall time            : {t_wall:.1f}s")
    print(f"Input tokens (sanity): {usage['input_tokens']:,}")
    print(f"  cache write        : {usage['cache_creation_input_tokens']:,}")
    print(f"  cache read         : {usage['cache_read_input_tokens']:,}")
    print(f"Output tokens        : {usage['output_tokens']:,}")
    print(f"Est. sanity LLM cost : ${estimate_cost(usage):.4f}")

    if rule_errors or happy_errors:
        print()
        print("Failures:")
        for e in rule_errors + happy_errors:
            print(f"  - {e}")

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
