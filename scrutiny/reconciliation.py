"""Reconciliation — Layer 5.

Takes a ``QueryPlan`` plus the list of ``GenerationResult`` objects
produced by Layer 3 and decides whether the sibling SQL paths agree
on the same business metric.

Design notes
------------
- The planner fans a metric out into N canonical definitions (e.g.,
  ``revenue_from_items_and_freight`` and ``revenue_from_payments``)
  upfront. Layer 5 does NOT re-prompt the generator for an
  alternative — it reconciles the siblings that are already there.
- Severity is decided deterministically from the delta magnitude and
  key-alignment check. The LLM's sole job is to write a one-sentence
  explanation of why the numbers agree or disagree.
- Multi-row siblings are joined on their grouping keys, never on row
  position. Key-alignment failure is severity="high" by itself,
  independent of delta magnitude.
- Dtype-inference picks the metric column: non-numeric columns are
  keys; the single numeric column is the metric. When a sibling has
  2+ numeric columns the first-pass inference is ambiguous, but the
  reconciler then falls back to cross-sibling disambiguation: if
  exactly one numeric column name appears in EVERY sibling, that
  shared name is used as the metric everywhere, and the extra
  numeric columns are ignored (neither key nor metric). This handles
  the common case where the SQL generator emits incidental aid
  columns (``order_count`` alongside ``total_revenue``) without
  tripping over them. Only truly ambiguous shapes (0 numeric
  columns, or 2+ numeric columns with no shared name) bail out
  with a structured failure reason rather than guessing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np
import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "reconciliation.md"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 512  # output is a single sentence in a JSON wrapper

PRICE_INPUT_PER_MTOK = 1.00
PRICE_CACHE_WRITE_PER_MTOK = 1.25
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 5.00

# Severity bands on symmetric percentage delta.
PASS_THRESHOLD = 0.02      # <= 2%  -> low
MEDIUM_THRESHOLD = 0.05    # > 2% and <= 5% -> medium ; > 5% -> high


Severity = Literal["low", "medium", "high"]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class SiblingSummary:
    """Presenter-safe summary of one sibling's contribution."""
    sub_question_id: int
    canonical_metric: str | None
    notes: str
    representative_value: Any  # scalar case: float; multi-row: list[dict]
    row_count: int
    metric_column: str | None
    key_columns: list[str]


# Taxonomy for *why* reconciliation was skipped. Downstream (Layer 6
# confidence) uses this to decide whether a skip is HIGH-eligible
# (single-path question backed by a documented canonical metric) or
# only MEDIUM-eligible (complementary views of one population, edge
# cases, etc.).
SkipCategory = Literal[
    "single_sub_question",    # plan has exactly 1 sub-question.
    "complementary_views",    # plan has >=2 sub-questions but the
                              # planner intentionally left
                              # reconciliation_step null (different
                              # slicings of one population — e.g.,
                              # "how does X relate to Y").
    "insufficient_siblings",  # planner wanted reconciliation but we
                              # didn't get >=2 sibling results (a
                              # sub-question failed upstream).
]


@dataclass
class ReconciliationResult:
    # Presenter-facing:
    skipped: bool
    passed: bool
    severity: Severity
    delta_pct: float | None   # representative (max for multi-row)
    note: str | None
    reason: str | None = None  # why skipped or why key-alignment failed

    # Internal / logging-only:
    shape: Literal["scalar", "multi_row", "skipped", "invalid"] = "skipped"
    key_alignment: Literal["ok", "failed", "n/a"] = "n/a"
    # Non-None only when ``skipped=True``. None on the non-skipped paths.
    skip_category: SkipCategory | None = None
    mean_delta_pct: float | None = None
    max_delta_pct: float | None = None
    missing_keys_a: list[Any] = field(default_factory=list)
    missing_keys_b: list[Any] = field(default_factory=list)
    sibling_summaries: list[SiblingSummary] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "passed": self.passed,
            "severity": self.severity,
            "delta_pct": self.delta_pct,
            "note": self.note,
            "reason": self.reason,
            "shape": self.shape,
            "key_alignment": self.key_alignment,
            "skip_category": self.skip_category,
            "mean_delta_pct": self.mean_delta_pct,
            "max_delta_pct": self.max_delta_pct,
            "missing_keys_a": self.missing_keys_a,
            "missing_keys_b": self.missing_keys_b,
        }


# ---------------------------------------------------------------------------
# Skip / trigger logic
# ---------------------------------------------------------------------------
def _should_skip(
    plan: Any, results: list[Any],
) -> tuple[bool, str | None, SkipCategory | None]:
    """Decide whether reconciliation is applicable for this plan.

    Returns ``(skipped, reason, skip_category)``.

    ``skipped=True`` means Layer 5 is a no-op. That is the correct
    behavior for single-path questions (e.g., unique customer counts)
    and for complementary-view relationship questions where the planner
    intentionally set ``reconciliation_step=null``. The ``skip_category``
    distinguishes those cases so Layer 6 can promote the former to HIGH
    confidence without raising the latter beyond MEDIUM.
    """
    reconciliation_step = getattr(plan, "reconciliation_step", None)
    sub_questions = list(getattr(plan, "sub_questions", []) or [])

    if not reconciliation_step:
        if len(sub_questions) <= 1:
            return (
                True,
                (
                    "Single-sub-question plan; reconciliation requires a "
                    "second SQL path, so this is a legitimate no-op."
                ),
                "single_sub_question",
            )
        return (
            True,
            (
                f"Planner emitted {len(sub_questions)} sub-questions but "
                "set reconciliation_step=null; treated as complementary "
                "views of one population (different bucketings/slicings "
                "are not cross-validation)."
            ),
            "complementary_views",
        )

    if len(results) < 2:
        return (
            True,
            (
                "Planner requested reconciliation but fewer than 2 "
                "sibling results arrived — an upstream sub-question "
                "failed. Treated as a skipped reconciliation."
            ),
            "insufficient_siblings",
        )
    return False, None, None


# ---------------------------------------------------------------------------
# Dtype-based key / metric inference
# ---------------------------------------------------------------------------
def _infer_columns(
    df: pd.DataFrame,
) -> tuple[list[str], str | None, str | None]:
    """Return (key_columns, metric_column, failure_reason).

    Rule:
      - non-numeric columns are keys
      - the single numeric column is the metric

    Ambiguous shapes return a failure_reason so the caller can bail out.
    """
    numeric = list(df.select_dtypes("number").columns)
    non_numeric = [c for c in df.columns if c not in numeric]

    if len(numeric) == 0:
        return non_numeric, None, (
            "No numeric column in result — nothing to reconcile."
        )
    if len(numeric) > 1:
        return non_numeric, None, (
            f"Multiple numeric columns ({numeric}); key/metric inference "
            "is ambiguous. Expected exactly one metric column."
        )
    return non_numeric, numeric[0], None


def _resolve_columns_across_siblings(
    frames: list[pd.DataFrame],
) -> list[tuple[list[str], str | None, str | None]]:
    """Per-sibling ``(key_columns, metric_column, failure_reason)``.

    First pass: run ``_infer_columns`` on each frame independently.
    If every sibling already has an unambiguous single numeric column
    we're done.

    If one or more siblings report *Multiple numeric columns*, fall
    back to **cross-sibling disambiguation**: look at the set of
    numeric column names present in each sibling, intersect them, and
    if exactly one name appears in *every* sibling, use that shared
    name as the metric everywhere. Extra numeric columns on any
    sibling are then ignored (treated as neither key nor metric).
    This handles the common case where the SQL generator emits an
    incidental aid column (``order_count`` alongside ``total_revenue``)
    without tripping the reconciler.

    Only truly ambiguous shapes — 0 numeric columns anywhere, or 2+
    numeric columns with no shared name — fall through unchanged and
    surface the original failure reason.
    """
    per_sibling = [_infer_columns(f) for f in frames]
    ambiguous = [
        i for i, (_, _, reason) in enumerate(per_sibling)
        if reason is not None and "Multiple numeric columns" in reason
    ]
    if not ambiguous:
        return per_sibling

    numeric_sets = [
        set(f.select_dtypes("number").columns) for f in frames
    ]
    if not numeric_sets:
        return per_sibling
    common = set.intersection(*numeric_sets)
    if len(common) != 1:
        # Either no shared name, or >1 shared name — still ambiguous.
        return per_sibling

    metric = next(iter(common))
    resolved: list[tuple[list[str], str | None, str | None]] = []
    for f in frames:
        numeric = set(f.select_dtypes("number").columns)
        # Keys are every column that isn't numeric in this frame.
        # Extra numeric columns (beyond the shared metric) are dropped
        # from both keys and metric — they're incidental aid columns.
        keys = [c for c in f.columns if c not in numeric]
        resolved.append((keys, metric, None))
    return resolved


# ---------------------------------------------------------------------------
# Symmetric percentage delta
# ---------------------------------------------------------------------------
def symmetric_delta(a: float, b: float) -> float:
    """Symmetric percentage difference between two numbers.

    ``|a - b| / ((|a| + |b|) / 2)``; both-zero returns 0.
    """
    a = float(a)
    b = float(b)
    denom = (abs(a) + abs(b)) / 2.0
    if denom == 0:
        # Both values are zero — they match.
        return 0.0
    return abs(a - b) / denom


def _severity_for_delta(delta_pct: float) -> Severity:
    if delta_pct <= PASS_THRESHOLD:
        return "low"
    if delta_pct <= MEDIUM_THRESHOLD:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Scalar reconciliation
# ---------------------------------------------------------------------------
def _is_scalar_shape(df: pd.DataFrame, metric_column: str | None) -> bool:
    """Scalar means 1 row, 1 metric column, 0 non-numeric key columns."""
    if metric_column is None:
        return False
    non_metric_cols = [c for c in df.columns if c != metric_column]
    return len(df) == 1 and len(non_metric_cols) == 0


def _reconcile_scalar(
    summaries: list[SiblingSummary],
) -> tuple[Severity, float, str | None]:
    """All siblings are scalars. Compute pairwise symmetric delta vs the
    first sibling and return (severity, representative_delta, reason).

    We use pairwise-vs-first rather than all-pairs because in practice
    the planner emits 2 siblings for cross-validation; generalizing
    further is YAGNI for Phase 1.
    """
    anchor = float(summaries[0].representative_value)
    max_delta = 0.0
    for other in summaries[1:]:
        d = symmetric_delta(anchor, float(other.representative_value))
        max_delta = max(max_delta, d)
    return _severity_for_delta(max_delta), max_delta, None


# ---------------------------------------------------------------------------
# Multi-row reconciliation
# ---------------------------------------------------------------------------
def _reconcile_multirow(
    frames: list[pd.DataFrame],
    key_columns: list[str],
    metric_columns: list[str],
) -> tuple[dict[str, Any], Severity, str | None]:
    """Join two+ frames on ``key_columns`` and compare per-row.

    Returns (stats, severity, reason). ``reason`` is non-None when key
    alignment fails or inference produced inconsistent keys across
    siblings.
    """
    # All siblings must have the same key column set. Order-insensitive.
    anchor_keys = set(key_columns)
    # frames' key columns are the caller's job to supply consistently;
    # we still defend against mismatches just in case.
    for f in frames:
        inferred = {c for c in f.columns if c not in f.select_dtypes("number").columns}
        if inferred != anchor_keys:
            return (
                {},
                "high",
                (
                    f"Key columns mismatch across siblings: {sorted(inferred)} "
                    f"vs {sorted(anchor_keys)}."
                ),
            )

    # Normalize each frame to (keys..., metric) named "__metric_i".
    normalized = []
    for i, (frame, mcol) in enumerate(zip(frames, metric_columns)):
        out = frame.loc[:, list(key_columns) + [mcol]].copy()
        out = out.rename(columns={mcol: f"__metric_{i}"})
        normalized.append(out)

    # Outer merge to expose missing keys.
    merged = normalized[0]
    for nxt in normalized[1:]:
        merged = merged.merge(nxt, on=key_columns, how="outer")

    # Detect missing keys (rows where any metric is NaN after outer merge
    # AND the other metrics are not).
    metric_cols = [f"__metric_{i}" for i in range(len(frames))]
    missing_mask = merged[metric_cols].isna().any(axis=1)
    missing_rows = merged[missing_mask]
    missing_keys_a: list[Any] = []
    missing_keys_b: list[Any] = []
    if len(missing_rows) > 0:
        # Report symmetric difference of present keys for the two-sibling
        # common case. We only surface the two-sided view; deeper fan-outs
        # will get a single "missing_rows" count.
        if len(frames) == 2:
            # Naming: missing_keys_X = keys ABSENT FROM sibling X.
            #   - rows where __metric_0 is NaN -> keys absent from sibling a.
            #   - rows where __metric_1 is NaN -> keys absent from sibling b.
            missing_keys_a = (
                missing_rows[missing_rows["__metric_0"].isna()][key_columns]
                .to_dict(orient="records")
            )
            missing_keys_b = (
                missing_rows[missing_rows["__metric_1"].isna()][key_columns]
                .to_dict(orient="records")
            )
        stats = {
            "max_delta_pct": None,
            "mean_delta_pct": None,
            "missing_keys_a": missing_keys_a,
            "missing_keys_b": missing_keys_b,
            "key_alignment": "failed",
        }
        reason = (
            f"{int(missing_mask.sum())} key(s) present in one sibling "
            "but not the other(s); per-row reconciliation is invalid."
        )
        return stats, "high", reason

    # Keys aligned. Compute per-row deltas as max-across-sibling-pairs.
    # With N=2 this collapses to one delta per row.
    per_row_deltas: list[float] = []
    for _, row in merged.iterrows():
        vals = [float(row[c]) for c in metric_cols]
        # Pairwise symmetric deltas; take max within the row.
        row_max = 0.0
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                row_max = max(row_max, symmetric_delta(vals[i], vals[j]))
        per_row_deltas.append(row_max)

    arr = np.array(per_row_deltas) if per_row_deltas else np.array([0.0])
    max_delta = float(arr.max())
    mean_delta = float(arr.mean())
    severity = _severity_for_delta(max_delta)

    stats = {
        "max_delta_pct": max_delta,
        "mean_delta_pct": mean_delta,
        "missing_keys_a": [],
        "missing_keys_b": [],
        "key_alignment": "ok",
    }
    return stats, severity, None


# ---------------------------------------------------------------------------
# Sibling summary extraction
# ---------------------------------------------------------------------------
def _summarize_sibling(result: Any) -> SiblingSummary:
    """Build a SiblingSummary for the LLM payload + internal logging.

    ``result`` is a ``GenerationResult`` from ``agents.sql_generator`` but
    we access attributes duck-typed so fixtures can stand in.
    """
    df: pd.DataFrame = result.dataframe
    keys, metric, _reason = _infer_columns(df)

    if _is_scalar_shape(df, metric):
        rep: Any = float(df.iloc[0][metric])  # type: ignore[index]
    elif metric is not None:
        rep = df.head(10).to_dict(orient="records")
    else:
        rep = df.head(5).to_dict(orient="records")

    # Pull canonical_metric from the sub_question if the generator attached
    # it (our current pipeline doesn't, but we keep the hook).
    canonical = getattr(result, "canonical_metric", None)

    return SiblingSummary(
        sub_question_id=getattr(result, "sub_question_id", -1),
        canonical_metric=canonical,
        notes=getattr(result, "notes", ""),
        representative_value=rep,
        row_count=getattr(result, "row_count", len(df)),
        metric_column=metric,
        key_columns=list(keys),
    )


def _attach_canonical_metric(
    summaries: list[SiblingSummary], plan: Any
) -> None:
    """Populate ``canonical_metric`` on each summary from the plan's
    sub_questions, matching by id.
    """
    sub_by_id = {
        getattr(sq, "id", None): sq for sq in getattr(plan, "sub_questions", [])
    }
    for s in summaries:
        if s.canonical_metric is None and s.sub_question_id in sub_by_id:
            sq = sub_by_id[s.sub_question_id]
            s.canonical_metric = getattr(sq, "canonical_metric", None)


# ---------------------------------------------------------------------------
# LLM call — writes the note only
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)


class LlmNote(BaseModel):
    model_config = ConfigDict(extra="allow")
    note: str


def _extract_json(text_blob: str) -> dict[str, Any]:
    blob = text_blob.strip()
    m = _FENCE_RE.search(blob)
    if m:
        blob = m.group(1)
    start = blob.find("{")
    end = blob.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in LLM output.")
    return json.loads(blob[start : end + 1])


def _run_llm_note(
    plan: Any,
    summaries: list[SiblingSummary],
    shape: str,
    stats: dict[str, Any],
    severity: Severity,
    delta_pct: float | None,
    *,
    client: Anthropic | None,
) -> tuple[str, dict[str, Any]]:
    if client is None:
        client = Anthropic()

    system_prompt = PROMPT_PATH.read_text()

    payload = {
        "restated_question": getattr(plan, "restated_question", ""),
        "reconciliation_step": getattr(plan, "reconciliation_step", ""),
        "siblings": [
            {
                "sub_question_id": s.sub_question_id,
                "canonical_metric": s.canonical_metric,
                "notes": s.notes,
                "representative_value": s.representative_value,
                "metric_column": s.metric_column,
                "key_columns": s.key_columns,
                "row_count": s.row_count,
            }
            for s in summaries
        ],
        "computed": {
            "shape": shape,
            "delta_pct": delta_pct,
            "max_delta_pct": stats.get("max_delta_pct"),
            "mean_delta_pct": stats.get("mean_delta_pct"),
            "severity": severity,
            "key_alignment": stats.get("key_alignment", "n/a"),
            "missing_keys_a": stats.get("missing_keys_a", []),
            "missing_keys_b": stats.get("missing_keys_b", []),
        },
    }

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "Write the reconciliation note. Return only the JSON "
                    "per the system prompt.\n\n"
                    "```json\n" + json.dumps(payload, indent=2, default=str)
                    + "\n```"
                ),
            }
        ],
    )

    u = response.usage
    usage = {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
    }

    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Reconciliation LLM stop_reason={response.stop_reason}")

    final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    data = _extract_json(final_text)
    try:
        parsed = LlmNote.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(f"LLM note output failed validation: {e}\nraw={data}")
    return parsed.note.strip(), usage


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def reconcile(
    plan: Any,
    results: list[Any],
    *,
    client: Anthropic | None = None,
    skip_llm: bool = False,
) -> ReconciliationResult:
    """Reconcile sibling SQL paths for a single cross-validation family.

    Returns a ``ReconciliationResult``. Skipped cases are valid (single-
    path questions); they return ``skipped=True, passed=True, severity="low"``
    with an explicit ``reason``.

    Set ``skip_llm=True`` to return without calling Haiku (used by the
    adversarial eval and fast unit tests).
    """
    skipped, skip_reason, skip_category = _should_skip(plan, results)
    if skipped:
        return ReconciliationResult(
            skipped=True,
            passed=True,
            severity="low",
            delta_pct=None,
            note=None,
            reason=skip_reason,
            shape="skipped",
            key_alignment="n/a",
            skip_category=skip_category,
        )

    # Build sibling summaries and attach canonical_metric from the plan.
    summaries = [_summarize_sibling(r) for r in results]
    _attach_canonical_metric(summaries, plan)

    # Per-sibling column inference with cross-sibling disambiguation:
    # if every sibling has exactly one numeric column, use it; if any
    # sibling has 2+ numeric columns but there's exactly one numeric
    # name shared across every sibling, use that shared name as the
    # metric everywhere (incidental aid columns like ``order_count``
    # alongside ``total_revenue`` get ignored). Truly ambiguous shapes
    # still bail out loudly below.
    per_sibling_infer: list[tuple[list[str], str | None, str | None]] = (
        _resolve_columns_across_siblings([r.dataframe for r in results])
    )
    for (_, metric, reason), r in zip(per_sibling_infer, results):
        if reason is not None:
            return ReconciliationResult(
                skipped=False,
                passed=False,
                severity="high",
                delta_pct=None,
                note=None,
                reason=(
                    f"Sibling sub_question_id={getattr(r, 'sub_question_id', '?')}: "
                    f"{reason}"
                ),
                shape="invalid",
                key_alignment="n/a",
                sibling_summaries=summaries,
            )

    # Determine scalar vs multi-row. All siblings must agree on shape.
    scalar_flags = [
        _is_scalar_shape(r.dataframe, m)
        for r, (_, m, _) in zip(results, per_sibling_infer)
    ]
    if all(scalar_flags):
        shape: Literal["scalar", "multi_row"] = "scalar"
    elif not any(scalar_flags):
        shape = "multi_row"
    else:
        return ReconciliationResult(
            skipped=False,
            passed=False,
            severity="high",
            delta_pct=None,
            note=None,
            reason=(
                "Sibling shape mismatch: some return a single scalar, "
                "others return multi-row tables; reconciliation is invalid."
            ),
            shape="invalid",
            key_alignment="n/a",
            sibling_summaries=summaries,
        )

    # Compute severity + delta deterministically.
    if shape == "scalar":
        severity, delta_pct, reason = _reconcile_scalar(summaries)
        stats = {
            "max_delta_pct": delta_pct,
            "mean_delta_pct": delta_pct,
            "missing_keys_a": [],
            "missing_keys_b": [],
            "key_alignment": "n/a",
        }
    else:
        # Multi-row: confirm all siblings agree on the key column set
        # before calling into the join logic.
        key_sets = [set(keys) for keys, _, _ in per_sibling_infer]
        if len({frozenset(s) for s in key_sets}) > 1:
            return ReconciliationResult(
                skipped=False,
                passed=False,
                severity="high",
                delta_pct=None,
                note=None,
                reason=(
                    f"Siblings disagree on key columns: "
                    f"{[sorted(s) for s in key_sets]}."
                ),
                shape="multi_row",
                key_alignment="failed",
                sibling_summaries=summaries,
            )
        key_columns = sorted(key_sets[0])
        metric_columns = [m for _, m, _ in per_sibling_infer]  # type: ignore[misc]
        frames = [r.dataframe for r in results]
        stats, severity, reason = _reconcile_multirow(
            frames, key_columns, metric_columns
        )
        delta_pct = stats.get("max_delta_pct")

    passed = severity != "high"

    # If we can't run the LLM (rule-only mode, or we already know the
    # answer is "invalid"), synthesize a deterministic note.
    note: str | None
    usage: dict[str, Any] = {}
    if skip_llm:
        note = _fallback_note(shape, severity, delta_pct, reason, summaries)
    else:
        try:
            note, usage = _run_llm_note(
                plan, summaries, shape, stats, severity, delta_pct, client=client
            )
        except Exception as e:
            note = (
                _fallback_note(shape, severity, delta_pct, reason, summaries)
                + f"  [LLM note unavailable: {type(e).__name__}]"
            )

    return ReconciliationResult(
        skipped=False,
        passed=passed,
        severity=severity,
        delta_pct=delta_pct,
        note=note,
        reason=reason,
        shape=shape,
        key_alignment=stats.get("key_alignment", "n/a"),
        mean_delta_pct=stats.get("mean_delta_pct"),
        max_delta_pct=stats.get("max_delta_pct"),
        missing_keys_a=stats.get("missing_keys_a", []),
        missing_keys_b=stats.get("missing_keys_b", []),
        sibling_summaries=summaries,
        usage=usage,
    )


def _fallback_note(
    shape: str,
    severity: Severity,
    delta_pct: float | None,
    reason: str | None,
    summaries: list[SiblingSummary],
) -> str:
    """Deterministic fallback when the LLM note isn't available."""
    if reason and severity == "high":
        return f"Reconciliation failed: {reason}"
    if delta_pct is None:
        return "Reconciliation produced no delta."
    pair = " vs ".join(
        (s.canonical_metric or f"sub_question_{s.sub_question_id}")
        for s in summaries
    )
    return (
        f"{pair} agree within {delta_pct:.2%} ({shape} shape, "
        f"severity={severity})."
    )


# ---------------------------------------------------------------------------
# Cost estimator
# ---------------------------------------------------------------------------
def estimate_cost(usage: dict[str, Any]) -> float:
    return (
        usage.get("input_tokens", 0) / 1_000_000 * PRICE_INPUT_PER_MTOK
        + usage.get("cache_creation_input_tokens", 0) / 1_000_000 * PRICE_CACHE_WRITE_PER_MTOK
        + usage.get("cache_read_input_tokens", 0) / 1_000_000 * PRICE_CACHE_READ_PER_MTOK
        + usage.get("output_tokens", 0) / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    )
