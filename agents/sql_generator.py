"""SQL Generator — Layer 3 agent.

Takes ONE planner sub-question and returns (a) an executable Postgres
SELECT, (b) a one-sentence rationale, and (c) the resulting DataFrame
with metadata.

Design notes
------------
- Single LLM call, no tools. Text-in, JSON-out: ``{sql, notes}``.
- Prompt caching on BOTH the system prompt and the data dictionary,
  same pattern as the Query Planner.
- sqlglot validates: single statement, SELECT only, no DDL/DML.
- Notes discipline enforced server-side: one sentence, <=25 words, no
  banned phrases ("I think", "confidence", etc.).
- Execution safety: wrapped in a `SET TRANSACTION READ ONLY` block;
  LIMIT auto-injected if missing.
- Returns a ``GenerationResult`` so downstream (scrutiny, presenter)
  can read sql+notes+df without re-parsing.

CLI
---
  python -m agents.sql_generator --sub-question '{"id":1,...}'
  python -m agents.sql_generator --from-plan "What was Q3 2017 revenue?"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text

from agents.query_planner import load_data_dictionary
from db.connection import get_engine

load_dotenv()

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "sql_generator.md"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048  # output is just {sql, notes}; 2K is generous

DEFAULT_ROW_CAP = 10_000
MAX_QUERY_SECONDS = 30  # statement timeout

# Rough Sonnet 4.6 pricing (USD per million tokens).
PRICE_INPUT_PER_MTOK = 3.00
PRICE_CACHE_WRITE_PER_MTOK = 3.75
PRICE_CACHE_READ_PER_MTOK = 0.30
PRICE_OUTPUT_PER_MTOK = 15.00

# Notes discipline — enforced server-side
NOTES_MAX_WORDS = 25
NOTES_BANNED_RE = re.compile(
    r"\b(i think|i believe|confidence|probably|maybe|might be|i'm not sure)\b",
    re.IGNORECASE,
)

# Positive whitelist: the root of the parsed statement must be one of
# these. Anything else (Insert, Update, Delete, Drop, Create, Alter,
# Truncate, Merge, ...) is rejected by exclusion. This is more robust
# than a deny list across sqlglot versions.
ALLOWED_ROOT_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------
class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class GeneratorOutput(_Permissive):
    """The raw JSON the LLM must emit."""
    sql: str
    notes: str


@dataclass
class GenerationResult:
    sub_question_id: int
    sql: str              # the SQL actually executed (post-LIMIT-injection)
    sql_raw: str          # the SQL the model emitted
    notes: str
    dataframe: pd.DataFrame
    row_count: int
    elapsed_ms: int
    limit_injected: bool
    usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)


def extract_json(text_blob: str) -> dict[str, Any]:
    blob = text_blob.strip()
    m = _FENCE_RE.search(blob)
    if m:
        blob = m.group(1)
    start = blob.find("{")
    end = blob.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output.")
    return json.loads(blob[start : end + 1])


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------
class SqlValidationError(ValueError):
    """SQL fails a guard check — reject before touching the DB."""


def validate_and_prepare_sql(
    sql: str, *, row_cap: int = DEFAULT_ROW_CAP
) -> tuple[str, bool]:
    """Parse, guard, and optionally inject LIMIT.

    Returns (final_sql, limit_injected).
    """
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlValidationError("Empty SQL.")

    # Reject multi-statement.
    statements = sqlglot.parse(sql, dialect="postgres")
    if len(statements) != 1:
        raise SqlValidationError(
            f"Expected a single SELECT; got {len(statements)} statements."
        )
    root = statements[0]
    if root is None:
        raise SqlValidationError("sqlglot could not parse SQL.")

    # Positive whitelist: root must be a Select/Union/Intersect/Except/
    # Subquery. Anything else (Insert, Update, Delete, Drop, Create,
    # Alter, Truncate, Merge, ...) fails here.
    if not isinstance(root, ALLOWED_ROOT_TYPES):
        raise SqlValidationError(
            f"Top-level statement must be SELECT-like; got {type(root).__name__}."
        )
    # Unwrap a top-level Subquery to the underlying Select for limit logic.
    top = root.this if isinstance(root, exp.Subquery) else root

    # LIMIT injection: only if absent AND the query could plausibly return
    # many rows. "Could plausibly return many rows" = no aggregate functions
    # or GROUP BY at the top level. Aggregate queries are self-bounded.
    has_limit = root.args.get("limit") is not None
    has_group_by = top.args.get("group") is not None
    has_aggregate = any(
        root.find(t) is not None
        for t in (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)
    )

    limit_injected = False
    if not has_limit and not (has_group_by or has_aggregate):
        root.set("limit", exp.Limit(expression=exp.Literal.number(row_cap)))
        limit_injected = True

    final_sql = root.sql(dialect="postgres")
    return final_sql, limit_injected


def validate_notes(notes: str) -> None:
    notes = notes.strip()
    if not notes:
        raise SqlValidationError("notes is empty.")
    # One sentence heuristic: at most one sentence-terminating mark,
    # and it must be at the end (or absent).
    terminators = [i for i, c in enumerate(notes) if c in ".!?"]
    if len(terminators) > 1 and terminators[-1] != len(notes) - 1:
        raise SqlValidationError(
            f"notes must be one sentence; got: {notes!r}"
        )
    words = notes.split()
    if len(words) > NOTES_MAX_WORDS:
        raise SqlValidationError(
            f"notes exceeds {NOTES_MAX_WORDS} words ({len(words)}): {notes!r}"
        )
    if NOTES_BANNED_RE.search(notes):
        raise SqlValidationError(
            f"notes contains banned hedging phrase: {notes!r}"
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _execute_readonly(engine, sql: str, *, timeout_s: int = MAX_QUERY_SECONDS) -> pd.DataFrame:
    """Run SQL in a read-only transaction with a statement timeout."""
    with engine.begin() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text(f"SET LOCAL statement_timeout = '{timeout_s}s'"))
        return pd.read_sql(text(sql), conn)


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------
def generate_and_execute(
    sub_question: dict[str, Any],
    data_dictionary: dict[str, Any],
    *,
    engine=None,
    client: Anthropic | None = None,
    row_cap: int = DEFAULT_ROW_CAP,
) -> GenerationResult:
    """Generate SQL for one sub-question, validate it, and execute it.

    ``sub_question`` is a dict matching the planner's ``SubQuestion``
    schema (the test harness typically passes ``.model_dump()`` output).
    """
    if client is None:
        client = Anthropic()
    if engine is None:
        engine = get_engine()

    system_prompt = PROMPT_PATH.read_text()
    dictionary_json = json.dumps(data_dictionary, indent=2)
    sub_q_json = json.dumps(sub_question, indent=2)

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
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "data_dictionary (ground truth):\n\n"
                            "```json\n" + dictionary_json + "\n```"
                        ),
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "sub_question:\n\n"
                            "```json\n" + sub_q_json + "\n```\n\n"
                            "Return only the JSON object per the system prompt."
                        ),
                    },
                ],
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
        raise RuntimeError("Generator hit max_tokens; bump MAX_TOKENS.")
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Unexpected stop_reason: {response.stop_reason}")

    final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    raw = extract_json(final_text)
    try:
        parsed = GeneratorOutput.model_validate(raw)
    except ValidationError as e:
        raise RuntimeError(f"Generator JSON failed validation: {e}\nRaw: {raw}")

    validate_notes(parsed.notes)
    final_sql, limit_injected = validate_and_prepare_sql(parsed.sql, row_cap=row_cap)

    t0 = time.time()
    df = _execute_readonly(engine, final_sql)
    elapsed_ms = int((time.time() - t0) * 1000)

    return GenerationResult(
        sub_question_id=int(sub_question.get("id", 0)),
        sql=final_sql,
        sql_raw=parsed.sql,
        notes=parsed.notes,
        dataframe=df,
        row_count=len(df),
        elapsed_ms=elapsed_ms,
        limit_injected=limit_injected,
        usage=usage,
    )


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------
def estimate_cost(usage: dict[str, Any]) -> float:
    return (
        usage.get("input_tokens", 0) / 1_000_000 * PRICE_INPUT_PER_MTOK
        + usage.get("cache_creation_input_tokens", 0) / 1_000_000 * PRICE_CACHE_WRITE_PER_MTOK
        + usage.get("cache_read_input_tokens", 0) / 1_000_000 * PRICE_CACHE_READ_PER_MTOK
        + usage.get("output_tokens", 0) / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sub-question",
        help="JSON string of one sub_question from the planner.",
    )
    parser.add_argument(
        "--from-plan",
        help="Shortcut: run the planner on this user question, then "
             "execute its first sub_question.",
    )
    parser.add_argument(
        "--print-sql", action="store_true",
        help="Print the final SQL after validation/injection.",
    )
    parser.add_argument(
        "--head", type=int, default=10,
        help="How many rows of the dataframe to display.",
    )
    args = parser.parse_args()

    if not (args.sub_question or args.from_plan):
        parser.error("Provide --sub-question or --from-plan.")

    data_dictionary = load_data_dictionary()

    if args.from_plan:
        # Lazy import to keep the generator usable without the planner present
        from agents.query_planner import plan_query
        plan, _ = plan_query(args.from_plan, data_dictionary)
        if not plan.answerable or not plan.sub_questions:
            print("Planner refused:", plan.unanswerable_reason)
            return 1
        sq = plan.sub_questions[0].model_dump()
        print(f"Using sub_question {sq['id']}: {sq['question'][:100]}")
    else:
        sq = json.loads(args.sub_question)

    result = generate_and_execute(sq, data_dictionary)

    print()
    print(f"Notes             : {result.notes}")
    print(f"LIMIT injected    : {result.limit_injected}")
    print(f"Rows              : {result.row_count:,}")
    print(f"Query time        : {result.elapsed_ms} ms")
    print(f"Input tokens      : {result.usage.get('input_tokens', 0):,}")
    print(f"  cache write     : {result.usage.get('cache_creation_input_tokens', 0):,}")
    print(f"  cache read      : {result.usage.get('cache_read_input_tokens', 0):,}")
    print(f"Output tokens     : {result.usage.get('output_tokens', 0):,}")
    print(f"Estimated cost    : ${estimate_cost(result.usage):.4f}")
    if args.print_sql:
        print()
        print("-- SQL --")
        print(result.sql)
    print()
    print(result.dataframe.head(args.head).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
