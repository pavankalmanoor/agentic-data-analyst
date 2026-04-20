"""Sanity Checks — Layer 4.

Deterministic rule-based checks (free) plus ONE Haiku call (judgment)
per sub-question result. Cross-validation reconciliation, narrative,
and presentation are NOT this layer's concern — they belong downstream.

Two-stage design
----------------
1. Python rules run first. They catch the obvious stuff (negative
   revenue, empty results, rates outside [0,1], high null rates).
   Rules are deterministic and free.
2. If no rule flag is severity="high", we ask Haiku: "does this look
   plausible for this sub-question?" Haiku returns a verdict and any
   judgment-level concerns the rules can't express.

``SanityResult.passed`` is True iff:
  - No rule flag at severity="high", AND
  - LLM (if called) returns ``plausible: true``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "sanity_check.md"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024  # concerns output is tiny

PRICE_INPUT_PER_MTOK = 1.00
PRICE_CACHE_WRITE_PER_MTOK = 1.25
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 5.00

# Column-name heuristics. Keep these tight — false positives cost more
# than false negatives here, because Haiku is the backstop for the
# judgment we can't encode.
MONETARY_RE = re.compile(
    r"(?:^|_)(revenue|price|payment|freight|gmv|sales|aov|avg.*value)"
    r"(?:_|$)",
    re.IGNORECASE,
)
RATE_RE = re.compile(r"(?:^|_)(rate|fraction|share|on_time)(?:_|$)", re.IGNORECASE)
PERCENT_RE = re.compile(r"(?:^|_)(pct|percent|percentage)(?:_|$)", re.IGNORECASE)
COUNT_RE = re.compile(
    r"(?:^|_)(count|n|num|total|customers|orders|reviews|products|sellers)"
    r"(?:_|$)",
    re.IGNORECASE,
)

NULL_RATE_THRESHOLD = 0.50


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
Severity = Literal["low", "medium", "high"]


@dataclass
class SanityFlag:
    rule: str
    column: str | None
    message: str
    severity: Severity


@dataclass
class SanityResult:
    passed: bool
    severity: Literal["none", "low", "medium", "high"]
    flags: list[SanityFlag] = field(default_factory=list)
    llm_severity: Literal["low", "medium", "high"] | None = None
    llm_verdict: str | None = None
    llm_concerns: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "severity": self.severity,
            "flags": [vars(f) for f in self.flags],
            "llm_severity": self.llm_severity,
            "llm_verdict": self.llm_verdict,
            "llm_concerns": self.llm_concerns,
        }


# ---------------------------------------------------------------------------
# Rule-based checks
# ---------------------------------------------------------------------------
def _rule_empty_result(df: pd.DataFrame, sub_question: dict[str, Any]) -> list[SanityFlag]:
    if len(df) > 0:
        return []
    # An empty result is always suspicious in the happy path. A later
    # layer may downgrade this for legitimate "no data" cases.
    return [
        SanityFlag(
            rule="empty_result",
            column=None,
            message="Query returned zero rows.",
            severity="high",
        )
    ]


def _rule_negative_monetary(df: pd.DataFrame) -> list[SanityFlag]:
    flags: list[SanityFlag] = []
    for col in df.select_dtypes("number").columns:
        if MONETARY_RE.search(col) and (df[col] < 0).any():
            flags.append(SanityFlag(
                rule="negative_monetary",
                column=col,
                message=f"{col} contains negative values (min={df[col].min():.2f}).",
                severity="high",
            ))
    return flags


def _rule_rate_out_of_range(df: pd.DataFrame) -> list[SanityFlag]:
    flags: list[SanityFlag] = []
    for col in df.select_dtypes("number").columns:
        if RATE_RE.search(col):
            s = df[col].dropna()
            if len(s) and (s.min() < 0 or s.max() > 1):
                flags.append(SanityFlag(
                    rule="rate_out_of_range",
                    column=col,
                    message=(
                        f"{col} outside [0,1]: "
                        f"min={s.min():.3f} max={s.max():.3f}."
                    ),
                    severity="high",
                ))
    return flags


def _rule_percent_out_of_range(df: pd.DataFrame) -> list[SanityFlag]:
    flags: list[SanityFlag] = []
    for col in df.select_dtypes("number").columns:
        if PERCENT_RE.search(col):
            s = df[col].dropna()
            if len(s) and (s.min() < 0 or s.max() > 100):
                flags.append(SanityFlag(
                    rule="percent_out_of_range",
                    column=col,
                    message=(
                        f"{col} outside [0,100]: "
                        f"min={s.min():.2f} max={s.max():.2f}."
                    ),
                    severity="high",
                ))
    return flags


def _rule_negative_count(df: pd.DataFrame) -> list[SanityFlag]:
    flags: list[SanityFlag] = []
    for col in df.select_dtypes("number").columns:
        if COUNT_RE.search(col) and (df[col] < 0).any():
            flags.append(SanityFlag(
                rule="negative_count",
                column=col,
                message=f"{col} contains negative values; counts should be >=0.",
                severity="high",
            ))
    return flags


def _rule_null_rate_high(df: pd.DataFrame) -> list[SanityFlag]:
    if len(df) == 0:
        return []
    flags: list[SanityFlag] = []
    for col in df.columns:
        null_rate = df[col].isna().mean()
        if null_rate > NULL_RATE_THRESHOLD:
            flags.append(SanityFlag(
                rule="null_rate_high",
                column=col,
                message=(
                    f"{col} is {null_rate:.0%} null (threshold "
                    f"{NULL_RATE_THRESHOLD:.0%})."
                ),
                severity="medium",
            ))
    return flags


def _rule_all_zero_numeric(df: pd.DataFrame) -> list[SanityFlag]:
    if len(df) == 0:
        return []
    flags: list[SanityFlag] = []
    for col in df.select_dtypes("number").columns:
        s = df[col].dropna()
        if len(s) > 0 and (s == 0).all():
            flags.append(SanityFlag(
                rule="all_zero_numeric",
                column=col,
                message=f"{col} is entirely zero across {len(s)} row(s).",
                severity="medium",
            ))
    return flags


RULE_CHECKS = (
    _rule_empty_result,
    _rule_negative_monetary,
    _rule_rate_out_of_range,
    _rule_percent_out_of_range,
    _rule_negative_count,
    _rule_null_rate_high,
    _rule_all_zero_numeric,
)


def run_rule_checks(df: pd.DataFrame, sub_question: dict[str, Any]) -> list[SanityFlag]:
    flags: list[SanityFlag] = []
    for check in RULE_CHECKS:
        try:
            # Two checks need the sub_question; the rest just need df
            if check is _rule_empty_result:
                flags.extend(check(df, sub_question))
            else:
                flags.extend(check(df))
        except Exception as e:
            flags.append(SanityFlag(
                rule=f"{check.__name__}_raised",
                column=None,
                message=f"{type(e).__name__}: {e}",
                severity="low",
            ))
    return flags


# ---------------------------------------------------------------------------
# Result summarization for the LLM
# ---------------------------------------------------------------------------
def summarize_result(df: pd.DataFrame) -> dict[str, Any]:
    """Compact, JSON-serializable summary of a result DataFrame."""
    head_records = df.head(5).to_dict(orient="records")
    # Convert non-JSON-native types to strings
    for row in head_records:
        for k, v in list(row.items()):
            if isinstance(v, (pd.Timestamp,)):
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
            "null_count": int(s.isna().sum()),
        }

    return {
        "row_count": int(len(df)),
        "columns": [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns],
        "head": head_records,
        "aggregates": aggregates,
    }


# ---------------------------------------------------------------------------
# LLM check
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)


class LlmVerdict(BaseModel):
    model_config = ConfigDict(extra="allow")
    severity: Literal["low", "medium", "high"]
    verdict: str
    concerns: list[str] = []


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


def run_llm_check(
    sub_question: dict[str, Any],
    result_summary: dict[str, Any],
    generator_notes: str,
    *,
    client: Anthropic | None = None,
) -> tuple[LlmVerdict, dict[str, Any]]:
    if client is None:
        client = Anthropic()

    system_prompt = PROMPT_PATH.read_text()
    payload = {
        "sub_question": sub_question,
        "result_summary": result_summary,
        "generator_notes": generator_notes,
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
                    "Judge this result. Return only the JSON per the system "
                    "prompt.\n\n"
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
        raise RuntimeError(f"Sanity LLM stop_reason={response.stop_reason}")

    final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    data = _extract_json(final_text)
    try:
        verdict = LlmVerdict.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(f"LLM sanity output failed validation: {e}\nraw={data}")
    return verdict, usage


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def check_result(
    sub_question: dict[str, Any],
    dataframe: pd.DataFrame,
    generator_notes: str,
    *,
    client: Anthropic | None = None,
    skip_llm: bool = False,
) -> SanityResult:
    """Run rules + (optional) LLM check; return a SanityResult.

    Set ``skip_llm=True`` to return after rule checks only (useful for
    unit tests of the rule layer).
    """
    flags = run_rule_checks(dataframe, sub_question)
    severities = {f.severity for f in flags}
    any_high = "high" in severities
    if any_high:
        result_severity: Literal["none", "low", "medium", "high"] = "high"
    elif "medium" in severities:
        result_severity = "medium"
    elif "low" in severities:
        result_severity = "low"
    else:
        result_severity = "none"

    if skip_llm or any_high:
        # If rules already flagged a high-severity issue, don't pay Haiku to
        # confirm; the result isn't trustworthy regardless.
        return SanityResult(
            passed=not any_high,
            severity=result_severity,
            flags=flags,
            llm_severity=None,
            llm_verdict=None,
            llm_concerns=[],
        )

    summary = summarize_result(dataframe)
    verdict, usage = run_llm_check(sub_question, summary, generator_notes, client=client)

    # Severity-based gating. The LLM keeps veto power, but must commit to
    # "high" severity to use it — distributional weirdness alone is
    # "medium" and does not block. This preserves the LLM's ability to
    # catch semantic errors (wrong time filter, join artifacts) that
    # rules can't, while keeping it from over-blocking legitimate-but-
    # unusual distributions.
    llm_blocks = verdict.severity == "high"
    passed = (not any_high) and (not llm_blocks)

    # Escalate the aggregate severity to whichever is worse.
    severity_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    aggregate = max(
        severity_rank[result_severity],
        severity_rank[verdict.severity],
    )
    result_severity = {
        v: k for k, v in severity_rank.items()
    }[aggregate]  # type: ignore[assignment]

    return SanityResult(
        passed=passed,
        severity=result_severity,
        flags=flags,
        llm_severity=verdict.severity,
        llm_verdict=verdict.verdict,
        llm_concerns=list(verdict.concerns),
        usage=usage,
    )


def estimate_cost(usage: dict[str, Any]) -> float:
    return (
        usage.get("input_tokens", 0) / 1_000_000 * PRICE_INPUT_PER_MTOK
        + usage.get("cache_creation_input_tokens", 0) / 1_000_000 * PRICE_CACHE_WRITE_PER_MTOK
        + usage.get("cache_read_input_tokens", 0) / 1_000_000 * PRICE_CACHE_READ_PER_MTOK
        + usage.get("output_tokens", 0) / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    )
