"""Layer 7 eval harness — N-trial runner.

Loads curated questions from ``eval/questions.jsonl`` and runs each
question through the full pipeline N times. The output is a
timestamped JSON blob written to ``eval/results/`` containing
everything Phase C (metrics) and Phase D (drift) will need:

- per-trial plan answerability
- per-trial SQL text per sub-question  (← drift analysis)
- per-trial confidence label + reason  (← label distribution)
- per-trial reconciliation delta_pct   (← delta distribution)
- per-trial presenter sections         (← hint substring-match later)
- per-trial cost breakdown + wall time (← cost P50/P95)
- per-layer raw usage dicts            (← token-level spot checks)

Design notes
------------
- The runner is intentionally sequential. Parallelism buys us very
  little here (the main cost is LLM wall time) and it would make
  error isolation fiddly.
- One trial crashing does NOT abort the harness. The error is logged
  into the trial record and the next trial starts clean.
- The harness never interprets results. It only records them.
  Metrics (pass/fail against expected_confidence, hint matching,
  cost percentiles, drift) happen in Phase C in a separate tool.

CLI
---
    python -m eval.runner                         # N=3, all questions
    python -m eval.runner --n-trials 1            # Phase B dry run
    python -m eval.runner --question-ids 1,7,13   # subset
    python -m eval.runner --output-dir eval/results/dryrun
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from agents.query_planner import load_data_dictionary
from db.connection import get_engine

from main import (
    RunMetrics,
    SubQuestionOutcome,
    run_pipeline,
)

load_dotenv()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTIONS = REPO_ROOT / "eval" / "questions.jsonl"
DEFAULT_RESULTS_DIR = REPO_ROOT / "eval" / "results"


# ---------------------------------------------------------------------------
# Question loading
# ---------------------------------------------------------------------------
def load_questions(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file of curated questions.

    Raises ``ValueError`` on malformed rows so the harness fails fast
    if the curated set drifts out of the expected schema.
    """
    if not path.exists():
        raise FileNotFoundError(f"Questions file not found: {path}")

    questions: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}: line {i} is not valid JSON: {e}"
                ) from e
            for key in ("id", "category", "question", "expected_confidence"):
                if key not in obj:
                    raise ValueError(
                        f"{path}: line {i} missing required field {key!r}"
                    )
            questions.append(obj)
    return questions


# ---------------------------------------------------------------------------
# Per-trial serialization
# ---------------------------------------------------------------------------
def _serialize_outcome(o: SubQuestionOutcome) -> dict[str, Any]:
    """Compact record for one sub-question outcome.

    We keep the SQL text (drift analysis), sanity shape (label
    distribution), and row_count (sanity on the shape of the result).
    We deliberately do NOT dump the full dataframe — it can be
    gigabytes, and we don't need it for any Phase C/D metric.
    """
    return {
        "sub_question_id": o.sub_question.id,
        "question": o.sub_question.question,
        "canonical_metric": o.sub_question.canonical_metric,
        "sql": o.generation.sql,
        "notes": o.generation.notes,
        "row_count": o.generation.row_count,
        "sanity": o.sanity.as_dict(),
        "retry_count": o.retry_count,
        "retries_exhausted": o.retries_exhausted,
    }


def _serialize_pipeline_result(
    plan, outcomes, reconciliation, confidence, presentation, metrics,
) -> dict[str, Any]:
    return {
        "plan": {
            "answerable": plan.answerable,
            "unanswerable_reason": plan.unanswerable_reason,
            "n_sub_questions": len(plan.sub_questions),
            "reconciliation_step": bool(plan.reconciliation_step),
            # Keep the full dump — cheap, small, useful for debugging
            # planner decisions later.
            "full": plan.model_dump(mode="json"),
        },
        "sub_questions": [_serialize_outcome(o) for o in outcomes],
        "reconciliation": (
            reconciliation.as_dict() if reconciliation is not None else None
        ),
        "confidence": confidence.as_dict(),
        "report": presentation.output.model_dump(),
        "usage": {
            "planner": metrics.planner_usage,
            "sql_generator": metrics.generator_usages,
            "sanity": metrics.sanity_usages,
            "reconciliation": metrics.reconciliation_usage,
            "presenter": metrics.presenter_usage,
        },
        "cost": metrics.total_cost(),
    }


# ---------------------------------------------------------------------------
# Per-trial execution (with error isolation)
# ---------------------------------------------------------------------------
def _run_single_trial(
    question_text: str,
    *,
    client: Anthropic,
    engine: Any,
    data_dictionary: dict[str, Any],
    max_retries: int,
) -> dict[str, Any]:
    """Run one trial and return a trial record.

    Records ``ok=False`` + a serialized exception instead of raising so
    the harness can continue.
    """
    t0 = time.time()
    try:
        plan, outcomes, recon, confidence, presentation, metrics = run_pipeline(
            question_text,
            client=client,
            engine=engine,
            data_dictionary=data_dictionary,
            max_retries=max_retries,
            verbose=False,
        )
    except Exception as e:  # noqa: BLE001 — harness-level isolation
        tb = traceback.format_exc()
        return {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": tb,
            },
            "wall_time_s": round(time.time() - t0, 3),
        }

    wall_s = round(time.time() - t0, 3)
    body = _serialize_pipeline_result(
        plan, outcomes, recon, confidence, presentation, metrics,
    )
    body["ok"] = True
    body["error"] = None
    body["wall_time_s"] = wall_s
    return body


# ---------------------------------------------------------------------------
# Summary print helpers (non-authoritative — Phase C is the metrics tool)
# ---------------------------------------------------------------------------
def _one_line_trial(trial: dict[str, Any]) -> str:
    if not trial.get("ok"):
        err = trial.get("error") or {}
        return f"ERROR {err.get('type', '?')}: {err.get('message', '')[:100]}"
    conf = trial.get("confidence") or {}
    cost = trial.get("cost") or {}
    plan = trial.get("plan") or {}
    recon = trial.get("reconciliation")
    bits = [
        f"{conf.get('label', '?'):<7s}",
        f"${cost.get('total', 0.0):.3f}",
        f"{trial.get('wall_time_s', 0):.1f}s",
    ]
    if plan.get("answerable"):
        bits.append(f"sq={plan.get('n_sub_questions', 0)}")
        if recon is not None:
            if recon.get("skipped"):
                bits.append("recon=skipped")
            else:
                d = recon.get("delta_pct")
                bits.append(f"delta={d:.4f}" if d is not None else "delta=?")
    else:
        bits.append("refused")
    return " ".join(bits)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# Main harness loop
# ---------------------------------------------------------------------------
def run_harness(
    *,
    questions_path: Path,
    output_dir: Path,
    n_trials: int,
    question_ids: list[int] | None,
    max_retries: int,
    verbose: bool,
) -> Path:
    """Run every selected question ``n_trials`` times, write JSON blob.

    Returns the path to the results file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    questions = load_questions(questions_path)
    if question_ids is not None:
        questions = [q for q in questions if q["id"] in set(question_ids)]
        if not questions:
            raise ValueError(
                f"--question-ids {question_ids!r} matched nothing in "
                f"{questions_path}"
            )

    # One client and one engine and one data dictionary across the
    # whole harness. The planner caches the data dictionary in its
    # system prompt, so reusing the client also preserves prompt
    # caching (if enabled on this client).
    client = Anthropic()
    engine = get_engine()
    data_dictionary = load_data_dictionary()

    started_at = _dt.datetime.now(_dt.timezone.utc)
    t0 = time.time()

    results: list[dict[str, Any]] = []
    for qi, q in enumerate(questions, start=1):
        print("=" * 78)
        print(
            f"[Question {qi}/{len(questions)}] id={q['id']} "
            f"category={q['category']}"
        )
        print(f"  Q: {q['question']}")
        print(f"  expected_confidence={q['expected_confidence']}  "
              f"scrutiny={q.get('expected_scrutiny_trigger')}")
        print("-" * 78)

        trials: list[dict[str, Any]] = []
        for t in range(n_trials):
            label = f"trial {t + 1}/{n_trials}"
            print(f"  [{label}] running...", flush=True)
            trial = _run_single_trial(
                q["question"],
                client=client,
                engine=engine,
                data_dictionary=data_dictionary,
                max_retries=max_retries,
            )
            trial["trial_idx"] = t
            trials.append(trial)
            print(f"    -> {_one_line_trial(trial)}")
            if not trial.get("ok") and verbose:
                err = trial.get("error") or {}
                print(err.get("traceback", "")[:2000])

        results.append({
            "id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "expected_confidence": q["expected_confidence"],
            "expected_scrutiny_trigger": q.get("expected_scrutiny_trigger"),
            "expected_answer_hint": q.get("expected_answer_hint"),
            "notes": q.get("notes"),
            "trials": trials,
        })

    finished_at = _dt.datetime.now(_dt.timezone.utc)
    wall_s = time.time() - t0

    blob = {
        "meta": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "wall_time_s": round(wall_s, 2),
            "n_trials": n_trials,
            "n_questions": len(questions),
            "question_ids": [q["id"] for q in questions],
            "questions_path": str(questions_path.relative_to(REPO_ROOT)),
            "max_retries": max_retries,
            "git_sha": _git_sha(),
            "schema_version": 1,
        },
        "questions": results,
    }

    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"{stamp}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2, default=str)

    _print_summary(blob, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Summary table (for eyeballs only — Phase C does the real metrics)
# ---------------------------------------------------------------------------
def _print_summary(blob: dict[str, Any], out_path: Path) -> None:
    meta = blob["meta"]
    rows = blob["questions"]

    total_cost = 0.0
    crashes = 0
    label_counts: dict[str, int] = {}
    within_expected = 0
    trial_total = 0

    print()
    print("=" * 78)
    print("Per-question summary")
    print("=" * 78)
    for r in rows:
        id_ = r["id"]
        exp = set(r["expected_confidence"])
        labels: list[str] = []
        costs: list[float] = []
        for trial in r["trials"]:
            trial_total += 1
            if not trial.get("ok"):
                crashes += 1
                labels.append("ERR")
                continue
            lab = (trial.get("confidence") or {}).get("label", "?")
            labels.append(lab)
            label_counts[lab] = label_counts.get(lab, 0) + 1
            if lab in exp:
                within_expected += 1
            total_cost += float((trial.get("cost") or {}).get("total", 0.0))
            costs.append(float(trial["cost"]["total"]))
        cost_str = (
            f"${sum(costs)/len(costs):.3f} avg" if costs else "—"
        )
        label_str = ",".join(labels)
        mark = "✓" if all((l in exp) for l in labels if l != "ERR") and "ERR" not in labels else "·"
        print(
            f"  {mark} id={id_:>2d}  labels=[{label_str}]  "
            f"expected={sorted(exp)}  {cost_str}"
        )

    print()
    print("Totals")
    print("-" * 78)
    print(f"  questions               : {len(rows)}")
    print(f"  trials (total)          : {trial_total}")
    print(f"  crashes                 : {crashes}")
    print(f"  label hits in expected  : {within_expected}/{trial_total - crashes}")
    print(f"  label distribution      : {label_counts}")
    print(f"  wall time               : {meta['wall_time_s']:.1f}s")
    print(f"  total cost              : ${total_cost:.4f}")
    print(f"  results written to      : {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_ids(s: str | None) -> list[int] | None:
    if not s:
        return None
    try:
        return [int(x.strip()) for x in s.split(",") if x.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--question-ids must be a comma-separated list of ints, got {s!r}"
        ) from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--questions", type=Path, default=DEFAULT_QUESTIONS,
        help=f"Path to questions JSONL (default: {DEFAULT_QUESTIONS})",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_RESULTS_DIR,
        help=f"Where to write the timestamped JSON blob "
             f"(default: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--n-trials", type=int, default=3,
        help="Number of trials per question (default: 3)",
    )
    parser.add_argument(
        "--question-ids", type=str, default=None,
        help="Optional comma-separated list of question IDs to run "
             "(default: all)",
    )
    parser.add_argument(
        "--max-retries", type=int, default=2,
        help="SQL-generator retry budget per sub-question (default: 2)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print exception tracebacks when a trial crashes.",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set in env (or .env). "
            "The pipeline cannot run.",
            file=sys.stderr,
        )
        return 2

    out = run_harness(
        questions_path=args.questions,
        output_dir=args.output_dir,
        n_trials=args.n_trials,
        question_ids=_parse_ids(args.question_ids),
        max_retries=args.max_retries,
        verbose=args.verbose,
    )
    print(f"\nDone. {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
