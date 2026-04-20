"""Query Planner — Layer 2 agent.

Reads /prompts/query_planner.md verbatim plus the cached data dictionary
at /db/data_dictionary.json, and turns a natural-language business question
into a validated JSON plan of 1–4 SQL-answerable sub-questions (or a clean
"unanswerable" verdict).

Design notes
------------
- The system prompt is loaded VERBATIM. Edit the .md, not this file.
- Prompt caching is applied to BOTH the system prompt and the data
  dictionary. The dictionary is the biggest reused context across
  planner invocations, so we put it in the user turn with
  ``cache_control`` and follow with the user's actual question
  (uncached). Subsequent questions within the cache TTL pay cache-read
  prices on the dictionary.
- No tools. The planner is pure text-in, JSON-out. Tool-use loops belong
  in Layer 1 (schema understander) and Layer 3 (SQL generator).
- Pydantic models mirror the output schema defined in
  /prompts/query_planner.md. They are permissive (``extra="allow"``) so
  that future additions to the prompt don't silently drop fields.

CLI
---
  python -m agents.query_planner "What was Q3 2017 revenue?"
  python -m agents.query_planner --question "..." --print
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

load_dotenv()

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "query_planner.md"
DICTIONARY_PATH = REPO_ROOT / "db" / "data_dictionary.json"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096  # plans are small — 1–4 sub-questions; 4K is generous

# Rough Sonnet 4.6 pricing (USD per million tokens). Used only for the
# end-of-run cost log — not billed here, just an estimate.
PRICE_INPUT_PER_MTOK = 3.00
PRICE_CACHE_WRITE_PER_MTOK = 3.75
PRICE_CACHE_READ_PER_MTOK = 0.30
PRICE_OUTPUT_PER_MTOK = 15.00


# ---------------------------------------------------------------------------
# Pydantic models — mirror /prompts/query_planner.md output format
# ---------------------------------------------------------------------------
class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class SubQuestion(_Permissive):
    id: int
    question: str
    canonical_metric: str | None = None
    tables_likely: list[str] = Field(default_factory=list)
    filters_implied: list[str] = Field(default_factory=list)
    aggregation_heavy: bool
    cross_validation_candidate: bool


class QueryPlan(_Permissive):
    restated_question: str
    answerable: bool
    unanswerable_reason: str | None = None
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    reconciliation_step: str | None = None


# ---------------------------------------------------------------------------
# JSON extraction — prompt says "output only JSON" but be robust to fences
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)


def extract_json(text_blob: str) -> dict[str, Any]:
    """Parse JSON out of a model response, stripping a code fence if present."""
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
# Core planner call
# ---------------------------------------------------------------------------
def plan_query(
    user_question: str,
    data_dictionary: dict[str, Any],
    *,
    client: Anthropic | None = None,
) -> tuple[QueryPlan, dict[str, Any]]:
    """Plan one user question against the data dictionary.

    Returns (plan, usage_dict). Raises RuntimeError if the model returns
    something we cannot parse or validate.
    """
    if client is None:
        client = Anthropic()

    system_prompt = PROMPT_PATH.read_text()
    dictionary_json = json.dumps(data_dictionary, indent=2)

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
                            "Here is the data_dictionary (ground truth about "
                            "the schema). Treat it as read-only context.\n\n"
                            "```json\n" + dictionary_json + "\n```"
                        ),
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": f"User question:\n\n{user_question.strip()}",
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
        raise RuntimeError(
            "Planner hit max_tokens before finishing. Bump MAX_TOKENS or "
            "check whether the model is over-decomposing."
        )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Unexpected stop_reason: {response.stop_reason}")

    final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not final_text:
        raise RuntimeError("Planner returned no text content.")

    data = extract_json(final_text)

    try:
        plan = QueryPlan.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(
            f"Planner output failed Pydantic validation.\n"
            f"Raw JSON:\n{json.dumps(data, indent=2)}\n\nErrors:\n{e}"
        ) from e

    _sanity_check(plan)
    return plan, usage


def _sanity_check(plan: QueryPlan) -> None:
    """Light structural checks that aren't expressible in Pydantic alone."""
    if plan.answerable:
        if plan.unanswerable_reason is not None:
            # Not strictly fatal — just unexpected. Raise to surface the bug.
            raise RuntimeError(
                "answerable=true but unanswerable_reason is set. "
                "Planner should pick one or the other."
            )
        if not plan.sub_questions:
            raise RuntimeError(
                "answerable=true but sub_questions is empty. "
                "A planner can't claim answerable without at least one sub-question."
            )
    else:
        if not plan.unanswerable_reason:
            raise RuntimeError(
                "answerable=false but unanswerable_reason is empty. "
                "Every refusal must cite a concrete reason."
            )
        if plan.sub_questions:
            # Rule 3 of the prompt: no "consolation prize" sub-questions.
            raise RuntimeError(
                "answerable=false but sub_questions is non-empty. "
                "Planner violated the no-consolation-prize rule."
            )

    seen_ids: set[int] = set()
    for sq in plan.sub_questions:
        if sq.id in seen_ids:
            raise RuntimeError(f"Duplicate sub_question id: {sq.id}")
        seen_ids.add(sq.id)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------
def estimate_cost(usage: dict[str, Any]) -> float:
    return (
        usage["input_tokens"] / 1_000_000 * PRICE_INPUT_PER_MTOK
        + usage["cache_creation_input_tokens"] / 1_000_000 * PRICE_CACHE_WRITE_PER_MTOK
        + usage["cache_read_input_tokens"] / 1_000_000 * PRICE_CACHE_READ_PER_MTOK
        + usage["output_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    )


# ---------------------------------------------------------------------------
# Dictionary loader
# ---------------------------------------------------------------------------
def load_data_dictionary() -> dict[str, Any]:
    if not DICTIONARY_PATH.exists():
        raise FileNotFoundError(
            f"Data dictionary not found at {DICTIONARY_PATH}. "
            f"Run: python -m agents.schema_understander"
        )
    return json.loads(DICTIONARY_PATH.read_text())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "question",
        nargs="?",
        help="The natural-language question to plan. "
             "If omitted, use --question.",
    )
    parser.add_argument(
        "--question", dest="question_flag",
        help="Alternate way to pass the question (useful for questions "
             "starting with a dash).",
    )
    parser.add_argument(
        "--print", dest="print_json", action="store_true",
        help="Print the full plan JSON to stdout.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the human-readable summary; implies --print.",
    )
    args = parser.parse_args()

    question = args.question or args.question_flag
    if not question:
        parser.error("Provide a question (positional or via --question).")

    data_dictionary = load_data_dictionary()

    t0 = time.time()
    plan, usage = plan_query(question, data_dictionary)
    elapsed = time.time() - t0

    if args.quiet or args.print_json:
        print(json.dumps(plan.model_dump(mode="json"), indent=2))

    if args.quiet:
        return 0

    print()
    print(f"Question          : {question}")
    print(f"Restated          : {plan.restated_question}")
    print(f"Answerable        : {plan.answerable}")
    if not plan.answerable:
        print(f"Reason            : {plan.unanswerable_reason}")
    else:
        print(f"Sub-questions     : {len(plan.sub_questions)}")
        for sq in plan.sub_questions:
            flags = []
            if sq.cross_validation_candidate:
                flags.append("xval")
            if sq.aggregation_heavy:
                flags.append("agg")
            flag_str = f" [{','.join(flags)}]" if flags else ""
            metric = f" ({sq.canonical_metric})" if sq.canonical_metric else ""
            print(f"  {sq.id}.{metric}{flag_str} {sq.question}")
        if plan.reconciliation_step:
            print(f"Reconciliation    : {plan.reconciliation_step}")
    print()
    print(f"Input tokens      : {usage['input_tokens']:,}")
    print(f"  cache write     : {usage['cache_creation_input_tokens']:,}")
    print(f"  cache read      : {usage['cache_read_input_tokens']:,}")
    print(f"Output tokens     : {usage['output_tokens']:,}")
    print(f"Estimated cost    : ${estimate_cost(usage):.4f}")
    print(f"Wall time         : {elapsed:.2f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
