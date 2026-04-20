"""Agentic Data Analyst — end-to-end orchestrator.

Wires every layer into a single user-question -> analyst-report flow:

  1. Layer 2 (planner)      : decompose the question into sub-questions.
  2. Layer 3 (sql_generator): per sub-question, generate+execute SQL.
  3. Layer 4 (sanity)       : per sub-question, deterministic rules +
                              Haiku judgment; retry SQL up to
                              ``MAX_RETRIES`` times if sanity fails.
  4. Layer 5 (reconcile)    : compare sibling SQL paths if the planner
                              emitted a reconciliation step.
  5. Layer 6 (confidence)   : deterministic HIGH/MEDIUM/LOW/UNABLE
                              label derived from the other layers'
                              outputs. No LLM call.
  6. Layer 6 (presenter)    : Sonnet narrates a four-section report
                              grounded in the full audit trail.

Design discipline
-----------------
- Retries live at the SQL-generator level only. A failed sanity check
  triggers one regenerate+re-execute, up to MAX_RETRIES total attempts
  per sub-question. Reconciliation failures do NOT trigger retries;
  they lower confidence instead (FOUNDATION §2.5).
- The planner's "unanswerable" verdict short-circuits everything: no
  generator calls, no sanity, no reconciliation — straight to the
  presenter with confidence=UNABLE.
- Every LLM call's usage is captured into ``RunMetrics`` so the CLI
  can print a per-layer cost roll-up.

CLI
---
  python -m main "What was Q3 2017 revenue?"
  python main.py "What was Q3 2017 revenue?" --print-json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from agents.query_planner import (
    QueryPlan,
    SubQuestion,
    estimate_cost as estimate_planner_cost,
    load_data_dictionary,
    plan_query,
)
from agents.presenter import (
    PresentationResult,
    estimate_cost as estimate_presenter_cost,
    present,
)
from agents.sql_generator import (
    GenerationResult,
    estimate_cost as estimate_generator_cost,
    generate_and_execute,
)
from db.connection import get_engine
from scrutiny.confidence import ConfidenceResult, derive_confidence
from scrutiny.reconciliation import (
    ReconciliationResult,
    estimate_cost as estimate_reconciliation_cost,
    reconcile,
)
from scrutiny.sanity import (
    SanityResult,
    check_result,
    estimate_cost as estimate_sanity_cost,
)

load_dotenv()


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------
MAX_RETRIES = 2  # number of regenerate attempts AFTER the first attempt
                 # (so total attempts per sub-question <= MAX_RETRIES + 1)


# ---------------------------------------------------------------------------
# Per-sub-question outcome
# ---------------------------------------------------------------------------
@dataclass
class SubQuestionOutcome:
    """What Layer 3+4 produced for one planner sub-question.

    Carries everything Layers 5, 6, and the presenter need. The
    ``retry_count`` counts regenerate attempts; 0 means the first SQL
    passed sanity. ``retries_exhausted`` is True only if the pipeline
    ran the full retry budget and sanity was still failing on the
    final attempt.
    """
    sub_question: SubQuestion
    generation: GenerationResult
    sanity: SanityResult
    retry_count: int
    retries_exhausted: bool

    def to_confidence_outcome(self) -> dict[str, Any]:
        return {
            "sub_question_id": self.sub_question.id,
            "sanity": self.sanity,
            "retry_count": self.retry_count,
            "retries_exhausted": self.retries_exhausted,
        }

    def to_presenter_outcome(self) -> dict[str, Any]:
        return {
            "sub_question": self.sub_question,
            "generation": self.generation,
            "sanity": self.sanity,
            "retry_count": self.retry_count,
        }


# ---------------------------------------------------------------------------
# Usage roll-up
# ---------------------------------------------------------------------------
@dataclass
class RunMetrics:
    planner_usage: dict[str, Any] = field(default_factory=dict)
    generator_usages: list[dict[str, Any]] = field(default_factory=list)
    sanity_usages: list[dict[str, Any]] = field(default_factory=list)
    reconciliation_usage: dict[str, Any] = field(default_factory=dict)
    presenter_usage: dict[str, Any] = field(default_factory=dict)

    def total_cost(self) -> dict[str, float]:
        parts = {
            "planner": estimate_planner_cost(self.planner_usage) if self.planner_usage else 0.0,
            "sql_generator": sum(
                estimate_generator_cost(u) for u in self.generator_usages
            ),
            "sanity": sum(estimate_sanity_cost(u) for u in self.sanity_usages),
            "reconciliation": (
                estimate_reconciliation_cost(self.reconciliation_usage)
                if self.reconciliation_usage else 0.0
            ),
            "presenter": (
                estimate_presenter_cost(self.presenter_usage)
                if self.presenter_usage else 0.0
            ),
        }
        parts["total"] = sum(parts.values())
        return parts


# ---------------------------------------------------------------------------
# Retry feedback assembly
# ---------------------------------------------------------------------------
def _build_retry_context(
    previous_generation: GenerationResult,
    previous_sanity: SanityResult,
) -> dict[str, Any]:
    """Structured retry feedback for ``generate_and_execute``.

    We hand the generator the severity, rule flags, verdict, and the
    previous SQL — no negotiation or policy prose. See
    ``sql_generator._format_retry_context`` for the exact shape.
    """
    return {
        "severity": previous_sanity.severity,
        "rule_flags": [
            f"{f.rule} on {f.column}" if f.column else f.rule
            for f in previous_sanity.flags
        ],
        "verdict": previous_sanity.llm_verdict,
        "concerns": list(previous_sanity.llm_concerns or []),
        "reason": (
            "Prior SQL returned a result that failed deterministic sanity "
            "checks. Regenerate with the canonical definition from the "
            "data dictionary."
        ),
        "previous_sql": previous_generation.sql,
    }


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------
def _run_sub_question(
    sub_question: SubQuestion,
    data_dictionary: dict[str, Any],
    *,
    engine: Any,
    client: Anthropic,
    metrics: RunMetrics,
    max_retries: int,
    verbose: bool,
) -> SubQuestionOutcome:
    """Generate SQL for one sub-question, run sanity, retry on failure.

    The loop exits as soon as sanity passes OR when we've used the full
    retry budget (``max_retries`` retries AFTER the first attempt).
    """
    last_generation: GenerationResult | None = None
    last_sanity: SanityResult | None = None
    retry_context: dict[str, Any] | None = None
    retry_count = 0

    # Attempts = 1 initial + up to max_retries retries.
    for attempt in range(max_retries + 1):
        if verbose:
            label = "attempt" if attempt == 0 else f"retry {attempt}"
            print(
                f"    [SQL] sub_question {sub_question.id}: "
                f"{label}..."
            )

        generation = generate_and_execute(
            sub_question.model_dump(),
            data_dictionary,
            engine=engine,
            client=client,
            retry_context=retry_context,
        )
        metrics.generator_usages.append(dict(generation.usage))

        sanity = check_result(
            sub_question.model_dump(),
            generation.dataframe,
            generation.notes,
            client=client,
        )
        metrics.sanity_usages.append(dict(sanity.usage))

        last_generation = generation
        last_sanity = sanity

        if verbose:
            flag_tokens = ",".join(f.rule for f in sanity.flags) or "clean"
            print(
                f"    [Sanity] sub_question {sub_question.id}: "
                f"passed={sanity.passed} severity={sanity.severity} "
                f"flags=[{flag_tokens}]"
            )

        if sanity.passed:
            break

        # Sanity failed; if we still have retries left, build feedback
        # and loop. Otherwise, break with retries_exhausted=True.
        if attempt == max_retries:
            break

        retry_context = _build_retry_context(generation, sanity)
        retry_count = attempt + 1

    assert last_generation is not None and last_sanity is not None

    retries_exhausted = (not last_sanity.passed) and (retry_count == max_retries)
    return SubQuestionOutcome(
        sub_question=sub_question,
        generation=last_generation,
        sanity=last_sanity,
        retry_count=retry_count,
        retries_exhausted=retries_exhausted,
    )


def run_pipeline(
    user_question: str,
    *,
    client: Anthropic | None = None,
    engine: Any = None,
    data_dictionary: dict[str, Any] | None = None,
    max_retries: int = MAX_RETRIES,
    verbose: bool = False,
) -> tuple[
    QueryPlan,
    list[SubQuestionOutcome],
    ReconciliationResult | None,
    ConfidenceResult,
    PresentationResult,
    RunMetrics,
]:
    """End-to-end run for a single user question.

    Returns the full tuple so callers (CLI, Streamlit, evals) can
    inspect any layer's output without re-running. The presenter
    result is the user-facing artifact; everything else is audit.
    """
    if client is None:
        client = Anthropic()
    if engine is None:
        engine = get_engine()
    if data_dictionary is None:
        data_dictionary = load_data_dictionary()

    metrics = RunMetrics()

    # -- Layer 2: plan ---------------------------------------------------
    if verbose:
        print("[Planner] decomposing question...")
    plan, planner_usage = plan_query(
        user_question, data_dictionary, client=client,
    )
    metrics.planner_usage = planner_usage

    if verbose:
        if plan.answerable:
            print(
                f"[Planner] answerable with {len(plan.sub_questions)} "
                f"sub-question(s); reconciliation_step="
                f"{bool(plan.reconciliation_step)}"
            )
        else:
            print(f"[Planner] declined: {plan.unanswerable_reason}")

    # Short-circuit: planner refusal. No generator, sanity, or
    # reconciliation. Go straight to the presenter with UNABLE.
    if not plan.answerable:
        confidence = derive_confidence(plan, [], None, max_retries=max_retries)
        if verbose:
            print(f"[Confidence] {confidence.label} — {confidence.reason}")
            print("[Presenter] composing report...")
        presentation = present(
            user_question=user_question,
            plan=plan,
            sub_outcomes=[],
            reconciliation=None,
            confidence=confidence,
            client=client,
        )
        metrics.presenter_usage = dict(presentation.usage)
        return plan, [], None, confidence, presentation, metrics

    # -- Layer 3 + 4: per sub-question generation with sanity + retry ---
    outcomes: list[SubQuestionOutcome] = []
    for sq in plan.sub_questions:
        outcome = _run_sub_question(
            sq, data_dictionary,
            engine=engine, client=client, metrics=metrics,
            max_retries=max_retries, verbose=verbose,
        )
        outcomes.append(outcome)

    # -- Layer 5: reconcile ---------------------------------------------
    reconciliation: ReconciliationResult | None
    if plan.reconciliation_step and len(outcomes) >= 2:
        if verbose:
            print("[Reconcile] cross-checking sibling paths...")
        reconciliation = reconcile(
            plan,
            [o.generation for o in outcomes],
            client=client,
        )
        metrics.reconciliation_usage = dict(reconciliation.usage)
        if verbose:
            print(
                f"[Reconcile] skipped={reconciliation.skipped} "
                f"passed={reconciliation.passed} "
                f"severity={reconciliation.severity} "
                f"delta_pct={reconciliation.delta_pct}"
            )
    else:
        reconciliation = reconcile(plan, [o.generation for o in outcomes])
        metrics.reconciliation_usage = dict(reconciliation.usage)
        if verbose:
            print(
                f"[Reconcile] skipped={reconciliation.skipped} "
                f"({reconciliation.reason})"
            )

    # -- Layer 6: confidence (deterministic) -----------------------------
    confidence = derive_confidence(
        plan,
        [o.to_confidence_outcome() for o in outcomes],
        reconciliation,
        max_retries=max_retries,
    )
    if verbose:
        print(f"[Confidence] {confidence.label} — {confidence.reason}")

    # -- Layer 6: presenter ---------------------------------------------
    if verbose:
        print("[Presenter] composing report...")
    presentation = present(
        user_question=user_question,
        plan=plan,
        sub_outcomes=[o.to_presenter_outcome() for o in outcomes],
        reconciliation=reconciliation,
        confidence=confidence,
        client=client,
    )
    metrics.presenter_usage = dict(presentation.usage)

    return plan, outcomes, reconciliation, confidence, presentation, metrics


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------
def _print_cost_summary(metrics: RunMetrics, elapsed_s: float) -> None:
    parts = metrics.total_cost()
    print("\n--- Cost summary ---")
    for k in ("planner", "sql_generator", "sanity", "reconciliation", "presenter"):
        print(f"  {k:<15s} ${parts[k]:.4f}")
    print(f"  {'TOTAL':<15s} ${parts['total']:.4f}")
    print(f"  Wall time     {elapsed_s:.2f}s")


def _print_run_json(
    plan: QueryPlan,
    outcomes: list[SubQuestionOutcome],
    reconciliation: ReconciliationResult | None,
    confidence: ConfidenceResult,
    presentation: PresentationResult,
) -> None:
    """Dump the full audit trail as a single JSON object.

    Used by ``--print-json`` and the end-to-end eval. Everything the
    pipeline produced, structured and reproducible.
    """
    blob = {
        "plan": plan.model_dump(mode="json"),
        "sub_questions": [
            {
                "id": o.sub_question.id,
                "question": o.sub_question.question,
                "canonical_metric": o.sub_question.canonical_metric,
                "sql": o.generation.sql,
                "notes": o.generation.notes,
                "row_count": o.generation.row_count,
                "sanity": o.sanity.as_dict(),
                "retry_count": o.retry_count,
                "retries_exhausted": o.retries_exhausted,
            }
            for o in outcomes
        ],
        "reconciliation": (
            reconciliation.as_dict() if reconciliation is not None else None
        ),
        "confidence": confidence.as_dict(),
        "report": presentation.output.model_dump(),
    }
    print(json.dumps(blob, indent=2, default=str))


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "question",
        nargs="?",
        help="Natural-language question to answer. If omitted, use --question.",
    )
    parser.add_argument(
        "--question", dest="question_flag",
        help="Alternate way to pass the question.",
    )
    parser.add_argument(
        "--print-json", action="store_true",
        help="Dump the full run as JSON after printing the report.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-layer progress logging.",
    )
    parser.add_argument(
        "--max-retries", type=int, default=MAX_RETRIES,
        help=f"Max retries per sub-question (default {MAX_RETRIES}).",
    )
    args = parser.parse_args()

    question = args.question or args.question_flag
    if not question:
        parser.error("Provide a question (positional or via --question).")

    t0 = time.time()
    plan, outcomes, reconciliation, confidence, presentation, metrics = run_pipeline(
        question,
        max_retries=args.max_retries,
        verbose=not args.quiet,
    )
    elapsed = time.time() - t0

    print()
    print(presentation.as_markdown())

    _print_cost_summary(metrics, elapsed)

    if args.print_json:
        print("\n--- Full run JSON ---")
        _print_run_json(plan, outcomes, reconciliation, confidence, presentation)

    return 0


if __name__ == "__main__":
    sys.exit(main())
