"""Presenter — Layer 6 final-narration agent.

Takes the full audit trail (planner plan + per-sub-question results
with sanity + retry counts + reconciliation + confidence) and produces
a validated four-section analyst report as JSON.

Design notes
------------
- System prompt is ``/prompts/presenter.md`` loaded verbatim. Edit the
  .md, not this file.
- Model: Claude Sonnet 4.6. Narration is Sonnet work per the
  cost-allocation decision in FOUNDATION §2.4.
- Prompt caching is applied to the system prompt. The user payload
  changes per question and is not cached.
- Output schema: ``PresenterOutput`` with four non-empty strings
  (``answer``, ``methodology``, ``verification``, ``caveats``).
- The presenter is a narrator. ``confidence.label`` arrives
  pre-computed; the prompt forbids the model from softening or
  overriding it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "presenter.md"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096  # four-section prose; 4K is generous

# Sonnet 4.6 pricing (per million tokens), for the per-run cost log.
PRICE_INPUT_PER_MTOK = 3.00
PRICE_CACHE_WRITE_PER_MTOK = 3.75
PRICE_CACHE_READ_PER_MTOK = 0.30
PRICE_OUTPUT_PER_MTOK = 15.00


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------
class PresenterOutput(BaseModel):
    """The strict four-section JSON the LLM must emit."""
    model_config = ConfigDict(extra="allow")

    answer: str
    methodology: str
    verification: str
    caveats: str


@dataclass
class PresentationResult:
    output: PresenterOutput
    confidence_label: str
    usage: dict[str, Any] = field(default_factory=dict)

    def as_markdown(self) -> str:
        """Render the four sections as a single markdown string.

        Useful for CLI printing; the Streamlit UI in Layer 8 will render
        each section in its own container instead of stitching.
        """
        o = self.output
        return (
            "## Answer\n\n"
            f"{o.answer}\n\n"
            "## Methodology\n\n"
            f"{o.methodology}\n\n"
            "## Verification\n\n"
            f"{o.verification}\n\n"
            "## Caveats\n\n"
            f"{o.caveats}\n"
        )


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------
def _summarize_dataframe(df: pd.DataFrame, max_rows: int = 10) -> dict[str, Any]:
    """Compact summary of a result DataFrame for the presenter payload.

    Keeps it small: shape + first ``max_rows`` rows + aggregates. The
    presenter does not need the raw rows to narrate — only to verify
    the number it cites exists in the data.
    """
    head_records: list[dict[str, Any]] = df.head(max_rows).to_dict(orient="records")
    for row in head_records:
        for k, v in list(row.items()):
            if isinstance(v, pd.Timestamp):
                row[k] = str(v)
            elif pd.isna(v):
                row[k] = None

    aggregates: dict[str, dict[str, Any]] = {}
    for col in df.select_dtypes("number").columns:
        s = df[col]
        aggregates[col] = {
            "min": float(s.min()) if s.notna().any() else None,
            "max": float(s.max()) if s.notna().any() else None,
            "mean": float(s.mean()) if s.notna().any() else None,
        }

    return {
        "shape": [int(df.shape[0]), int(df.shape[1])],
        "head": head_records,
        "aggregates": aggregates,
    }


def _headline_value(df: pd.DataFrame) -> Any:
    """Pick the canonical 'headline' value from a result frame.

    Heuristic, intentionally simple:
      - Single row, single numeric column -> scalar.
      - Single row, mix of columns        -> dict of that row.
      - Multi-row                         -> list[dict] of head rows.
    """
    if df.empty:
        return None
    numeric_cols = list(df.select_dtypes("number").columns)
    if len(df) == 1 and len(df.columns) == 1 and numeric_cols:
        return float(df.iloc[0][numeric_cols[0]])
    if len(df) == 1:
        row = df.iloc[0].to_dict()
        return {
            k: (None if pd.isna(v) else
                str(v) if isinstance(v, pd.Timestamp) else v)
            for k, v in row.items()
        }
    return _summarize_dataframe(df, max_rows=5)["head"]


def _serialize_sub_question(
    sub_question: Any,
    generation: Any,
    sanity: Any,
    retry_count: int,
) -> dict[str, Any]:
    """Build a presenter-payload dict for one executed sub-question."""
    sq = sub_question
    return {
        "id": getattr(sq, "id", None),
        "question": getattr(sq, "question", ""),
        "canonical_metric": getattr(sq, "canonical_metric", None),
        "filters_implied": list(getattr(sq, "filters_implied", []) or []),
        "aggregation_heavy": bool(getattr(sq, "aggregation_heavy", False)),
        "cross_validation_candidate": bool(
            getattr(sq, "cross_validation_candidate", False)
        ),
        "sql": getattr(generation, "sql", ""),
        "notes": getattr(generation, "notes", ""),
        "row_count": int(getattr(generation, "row_count", 0)),
        "headline": _headline_value(getattr(generation, "dataframe", pd.DataFrame())),
        "result_summary": _summarize_dataframe(
            getattr(generation, "dataframe", pd.DataFrame())
        ),
        "sanity": {
            "passed": bool(getattr(sanity, "passed", True)),
            "severity": getattr(sanity, "severity", "none"),
            "flags": [
                {
                    "rule": getattr(f, "rule", ""),
                    "column": getattr(f, "column", None),
                    "message": getattr(f, "message", ""),
                    "severity": getattr(f, "severity", "low"),
                }
                for f in getattr(sanity, "flags", [])
            ],
            "llm_verdict": getattr(sanity, "llm_verdict", None),
            "llm_concerns": list(getattr(sanity, "llm_concerns", []) or []),
        },
        "retry_count": int(retry_count),
    }


def _serialize_reconciliation(recon: Any) -> dict[str, Any]:
    """Presenter-safe view of the reconciliation result.

    Strips internal debug fields (mean_delta_pct, missing_keys lists,
    sibling_summaries) — the presenter should narrate from the
    user-facing fields only.
    """
    if recon is None:
        return {"skipped": True, "passed": True, "severity": "low",
                "note": None, "reason": "no reconciliation run"}
    return {
        "skipped": bool(getattr(recon, "skipped", True)),
        "passed": bool(getattr(recon, "passed", True)),
        "severity": getattr(recon, "severity", "low"),
        "delta_pct": getattr(recon, "delta_pct", None),
        "note": getattr(recon, "note", None),
        "reason": getattr(recon, "reason", None),
        "shape": getattr(recon, "shape", "skipped"),
    }


def _serialize_confidence(conf: Any) -> dict[str, Any]:
    if is_dataclass(conf):
        return {
            "label": getattr(conf, "label", "UNABLE"),
            "reason": getattr(conf, "reason", ""),
        }
    return {
        "label": conf.get("label", "UNABLE"),
        "reason": conf.get("reason", ""),
    }


def build_presenter_payload(
    user_question: str,
    plan: Any,
    sub_outcomes: list[dict[str, Any]],
    reconciliation: Any,
    confidence: Any,
) -> dict[str, Any]:
    """Assemble the compact JSON payload passed to the presenter LLM.

    ``sub_outcomes`` is a list of dicts with keys:
      - ``sub_question``  (planner SubQuestion or dict-like)
      - ``generation``    (GenerationResult)
      - ``sanity``        (SanityResult)
      - ``retry_count``   (int)
    """
    return {
        "user_question": user_question,
        "plan": {
            "restated_question": getattr(plan, "restated_question", ""),
            "answerable": bool(getattr(plan, "answerable", True)),
            "unanswerable_reason": getattr(plan, "unanswerable_reason", None),
            "reconciliation_step": getattr(plan, "reconciliation_step", None),
        },
        "sub_questions": [
            _serialize_sub_question(
                so["sub_question"],
                so["generation"],
                so["sanity"],
                so.get("retry_count", 0),
            )
            for so in sub_outcomes
        ],
        "reconciliation": _serialize_reconciliation(reconciliation),
        "confidence": _serialize_confidence(confidence),
    }


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)


def _extract_json(text_blob: str) -> dict[str, Any]:
    blob = text_blob.strip()
    m = _FENCE_RE.search(blob)
    if m:
        blob = m.group(1)
    start = blob.find("{")
    end = blob.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in presenter output.")
    return json.loads(blob[start : end + 1])


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def present(
    user_question: str,
    plan: Any,
    sub_outcomes: list[dict[str, Any]],
    reconciliation: Any,
    confidence: Any,
    *,
    client: Anthropic | None = None,
) -> PresentationResult:
    """Produce the four-section analyst report for a completed pipeline run.

    The return is validated. If the LLM's output fails schema validation
    or misses a section, a RuntimeError is raised — the presenter is the
    last step, so we'd rather fail loud than emit a partial report.
    """
    if client is None:
        client = Anthropic()

    system_prompt = PROMPT_PATH.read_text()
    payload = build_presenter_payload(
        user_question, plan, sub_outcomes, reconciliation, confidence,
    )

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
                    "Produce the four-section analyst report for this run. "
                    "Return only the JSON object per the system prompt.\n\n"
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
        "stop_reason": response.stop_reason,
    }

    if response.stop_reason == "max_tokens":
        raise RuntimeError("Presenter hit max_tokens; bump MAX_TOKENS.")
    if response.stop_reason != "end_turn":
        raise RuntimeError(
            f"Presenter unexpected stop_reason: {response.stop_reason}"
        )

    final_text = "\n".join(
        b.text for b in response.content if b.type == "text"
    ).strip()
    raw = _extract_json(final_text)
    try:
        output = PresenterOutput.model_validate(raw)
    except ValidationError as e:
        raise RuntimeError(
            f"Presenter JSON failed validation: {e}\nRaw: {raw}"
        )
    _validate_non_empty_sections(output)

    confidence_label = _serialize_confidence(confidence)["label"]
    return PresentationResult(
        output=output, confidence_label=confidence_label, usage=usage,
    )


def _validate_non_empty_sections(output: PresenterOutput) -> None:
    """Enforce that all four sections have content.

    The prompt requires non-empty strings. If the LLM emits blank
    sections (which would happen most naturally on an UNABLE response
    that the model misread as "omit everything"), we reject it so the
    orchestrator surfaces the bug rather than shipping a broken report.
    """
    missing = []
    for field_name in ("answer", "methodology", "verification", "caveats"):
        value = getattr(output, field_name, "")
        if not value or not value.strip():
            missing.append(field_name)
    if missing:
        raise RuntimeError(
            f"Presenter output is missing required section(s): {missing}"
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
