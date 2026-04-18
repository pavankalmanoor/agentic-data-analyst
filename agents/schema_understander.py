"""Schema Understander — Layer 1 agent.

Reads /prompts/schema_understander.md verbatim, gives Claude Haiku 4.5 a set
of schema-exploration tools, and produces a validated JSON data dictionary
saved to /db/data_dictionary.json.

Design notes
------------
- The system prompt is loaded VERBATIM. Do not edit it here; edit the .md.
- Prompt caching is enabled on the system prompt so multi-turn tool-use
  loops pay cache-read pricing on the biggest reused context.
- A schema hash (of table + column names + types) is sidecar'd at
  /db/.data_dictionary.schema_hash. The dictionary is only regenerated
  if the hash changes or --force is passed.
- Tool arguments (table/column names) are validated against a strict
  identifier regex before interpolation. The tools are exposed to the
  LLM; treat them with the paranoia you'd give any eval-string path.

CLI
---
  python -m agents.schema_understander            # regenerate only if schema changed
  python -m agents.schema_understander --force    # always regenerate
  python -m agents.schema_understander --print    # also print the full JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from db.connection import get_engine

load_dotenv()

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "schema_understander.md"
OUTPUT_PATH = REPO_ROOT / "db" / "data_dictionary.json"
HASH_PATH = REPO_ROOT / "db" / ".data_dictionary.schema_hash"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 16384  # dictionary output is big — 8K truncates mid-JSON
MAX_TURNS = 40  # safety cap on the tool-use loop

# Rough Haiku 4.5 pricing (USD per million tokens). Used only for the
# end-of-run cost log — not billed here, just an estimate.
PRICE_INPUT_PER_MTOK = 1.00
PRICE_CACHE_WRITE_PER_MTOK = 1.25
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 5.00

IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Allow only conservative SQL identifiers — the LLM calls these tools."""
    if not IDENTIFIER_RE.match(name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _tool_list_tables(engine) -> list[str]:
    sql = text(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    with engine.connect() as conn:
        return [row[0] for row in conn.execute(sql)]


def _tool_describe_table(engine, name: str) -> dict[str, Any]:
    name = _safe_ident(name)
    with engine.connect() as conn:
        cols = [
            dict(row._mapping)
            for row in conn.execute(
                text(
                    """
                    SELECT column_name, data_type, is_nullable, column_default,
                           character_maximum_length, numeric_precision, numeric_scale
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :name
                    ORDER BY ordinal_position
                    """
                ),
                {"name": name},
            )
        ]
        pk_cols = [
            row[0]
            for row in conn.execute(
                text(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_schema = 'public'
                      AND tc.table_name = :name
                    ORDER BY kcu.ordinal_position
                    """
                ),
                {"name": name},
            )
        ]
        fks = [
            dict(row._mapping)
            for row in conn.execute(
                text(
                    """
                    SELECT kcu.column_name,
                           ccu.table_name  AS foreign_table,
                           ccu.column_name AS foreign_column
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = 'public'
                      AND tc.table_name = :name
                    """
                ),
                {"name": name},
            )
        ]
        row_count = conn.execute(
            text(f'SELECT COUNT(*) FROM "{name}"')  # quoted ident; name validated above
        ).scalar_one()
    return {
        "table": name,
        "row_count": int(row_count),
        "columns": cols,
        "primary_key": pk_cols,
        "foreign_keys": fks,
    }


def _tool_sample_rows(engine, table: str, n: int = 5) -> list[dict[str, Any]]:
    table = _safe_ident(table)
    n = max(1, min(int(n), 50))
    with engine.connect() as conn:
        result = conn.execute(text(f'SELECT * FROM "{table}" LIMIT :n'), {"n": n})
        return [dict(row._mapping) for row in result]


def _tool_value_distribution(
    engine, table: str, column: str, top_n: int = 10
) -> list[dict[str, Any]]:
    table = _safe_ident(table)
    column = _safe_ident(column)
    top_n = max(1, min(int(top_n), 100))
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f'SELECT "{column}" AS value, COUNT(*) AS n '
                f'FROM "{table}" GROUP BY "{column}" '
                f'ORDER BY n DESC NULLS LAST LIMIT :n'
            ),
            {"n": top_n},
        )
        return [dict(row._mapping) for row in result]


TOOLS_SPEC = [
    {
        "name": "list_tables",
        "description": "List all table names in the public schema.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_table",
        "description": (
            "Get columns (with types, nullability, lengths), primary key, "
            "foreign keys, and row count for one table."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "sample_rows",
        "description": "Return up to N (default 5, max 50) rows from a table.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["table"],
        },
    },
    {
        "name": "value_distribution",
        "description": (
            "Return the top-N most frequent values for a column, with counts. "
            "Use for categorical columns to see value sets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "column": {"type": "string"},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["table", "column"],
        },
    },
]


def _execute_tool(engine, name: str, tool_input: dict[str, Any]) -> Any:
    """Dispatch a tool call from the LLM; return value is JSON-serialisable."""
    if name == "list_tables":
        return _tool_list_tables(engine)
    if name == "describe_table":
        return _tool_describe_table(engine, **tool_input)
    if name == "sample_rows":
        return _tool_sample_rows(engine, **tool_input)
    if name == "value_distribution":
        return _tool_value_distribution(engine, **tool_input)
    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Pydantic schema — loose on purpose; the LLM's JSON is the source of truth
# and we want to accept extra fields rather than reject useful additions.
# ---------------------------------------------------------------------------
class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class Grain(_Permissive):
    level: str
    description: str
    duplicates_possible: bool
    unique_on: list[str] = []


class ForeignKey(_Permissive):
    column: str
    references: str
    cardinality: str | None = None


class Column(_Permissive):
    type: str
    semantics: str
    nullable: bool | None = None
    null_rate_note: str | None = None
    gotchas: str | None = None


class Join(_Permissive):
    join_to: str
    on: str
    type: str | None = None
    note: str | None = None


class Table(_Permissive):
    purpose: str
    grain: Grain
    primary_key: str | list[str]
    foreign_keys: list[ForeignKey] = []
    columns: dict[str, Column]
    common_joins: list[Join] = []
    aggregation_notes: list[str] = []
    typical_questions: list[str] = []


class MetricDefinition(_Permissive):
    name: str
    sql_sketch: str
    use_when: str


class CanonicalMetric(_Permissive):
    description: str
    definitions: list[MetricDefinition]
    expected_delta: str | None = None


class Dataset(_Permissive):
    name: str
    description: str
    time_coverage: str
    row_counts_summary: dict[str, int] = Field(default_factory=dict)


class DataDictionary(_Permissive):
    dataset: Dataset
    tables: dict[str, Table]
    cardinalities: dict[str, str] = Field(default_factory=dict)
    canonical_metrics: dict[str, CanonicalMetric] = Field(default_factory=dict)
    cross_table_gotchas: list[str] = []
    unanswerable_question_hints: list[str] = []


# ---------------------------------------------------------------------------
# Schema hashing — decides whether we need to regenerate
# ---------------------------------------------------------------------------
def compute_schema_hash(engine) -> str:
    """Hash of table names + column names/types. Cheap to compute."""
    tables = _tool_list_tables(engine)
    sketch: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    with engine.connect() as conn:
        for t in tables:
            cols = [
                (row[0], row[1])
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = :n
                        ORDER BY ordinal_position
                        """
                    ),
                    {"n": t},
                )
            ]
            sketch.append((t, tuple(cols)))
    payload = json.dumps(sketch, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# JSON extraction — Haiku is instructed to output only JSON, but be robust
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)


def extract_json(text_blob: str) -> dict[str, Any]:
    """Parse JSON out of a model response, stripping a code fence if present."""
    m = _FENCE_RE.search(text_blob.strip())
    if m:
        text_blob = m.group(1)
    start = text_blob.find("{")
    end = text_blob.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output.")
    return json.loads(text_blob[start : end + 1])


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def run_agent(engine, *, verbose: bool = True) -> tuple[DataDictionary, dict[str, Any]]:
    """Run the multi-turn tool-use loop and return (dictionary, usage)."""
    system_prompt = PROMPT_PATH.read_text()
    client = Anthropic()

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Generate the data dictionary for this Postgres database. "
                "Follow every rule in the system prompt exactly."
            ),
        }
    ]

    totals = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "turns": 0,
        "tool_calls": 0,
    }
    final_text: str | None = None

    for turn in range(MAX_TURNS):
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
            tools=TOOLS_SPEC,
            messages=messages,
        )

        u = response.usage
        totals["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        totals["cache_creation_input_tokens"] += (
            getattr(u, "cache_creation_input_tokens", 0) or 0
        )
        totals["cache_read_input_tokens"] += (
            getattr(u, "cache_read_input_tokens", 0) or 0
        )
        totals["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        totals["turns"] += 1

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            texts = [b.text for b in response.content if b.type == "text"]
            final_text = "\n".join(texts).strip()
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                totals["tool_calls"] += 1
                if verbose:
                    print(
                        f"  [turn {turn + 1}] tool: {block.name}({json.dumps(block.input)})"
                    )
                try:
                    result = _execute_tool(engine, block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        }
                    )
                except Exception as e:  # surface errors to the model, don't crash
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": f"{type(e).__name__}: {e}",
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue

        raise RuntimeError(f"Unexpected stop_reason: {response.stop_reason}")

    if final_text is None:
        raise RuntimeError(
            f"Agent loop hit MAX_TURNS={MAX_TURNS} without a final response."
        )

    data = extract_json(final_text)
    try:
        dictionary = DataDictionary.model_validate(data)
    except ValidationError as e:
        # Save raw JSON alongside so we can debug without re-running.
        raw_path = OUTPUT_PATH.with_suffix(".raw.json")
        raw_path.write_text(json.dumps(data, indent=2, default=str))
        raise RuntimeError(
            f"Pydantic validation failed. Raw JSON saved to {raw_path}.\n{e}"
        ) from e

    return dictionary, totals


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------
def estimate_cost(totals: dict[str, Any]) -> float:
    return (
        totals["input_tokens"] / 1_000_000 * PRICE_INPUT_PER_MTOK
        + totals["cache_creation_input_tokens"] / 1_000_000 * PRICE_CACHE_WRITE_PER_MTOK
        + totals["cache_read_input_tokens"] / 1_000_000 * PRICE_CACHE_READ_PER_MTOK
        + totals["output_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_MTOK
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate even if the schema hash matches the cached one.",
    )
    parser.add_argument(
        "--print", dest="print_json", action="store_true",
        help="Print the full dictionary JSON to stdout after saving.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-turn tool-call logging.",
    )
    args = parser.parse_args()

    engine = get_engine()
    current_hash = compute_schema_hash(engine)

    if (
        not args.force
        and OUTPUT_PATH.exists()
        and HASH_PATH.exists()
        and HASH_PATH.read_text().strip() == current_hash
    ):
        print(f"Schema unchanged (hash {current_hash[:12]}). "
              f"Using cached {OUTPUT_PATH}. Pass --force to regenerate.")
        if args.print_json:
            print(OUTPUT_PATH.read_text())
        return 0

    print(f"Regenerating data dictionary (schema hash {current_hash[:12]})...")
    t0 = time.time()
    dictionary, totals = run_agent(engine, verbose=not args.quiet)
    elapsed = time.time() - t0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(dictionary.model_dump(mode="json"), indent=2, default=str)
    )
    HASH_PATH.write_text(current_hash)

    print()
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes)")
    print(f"Tables documented : {len(dictionary.tables)}")
    print(f"Canonical metrics : {len(dictionary.canonical_metrics)}")
    print(f"Turns             : {totals['turns']}")
    print(f"Tool calls        : {totals['tool_calls']}")
    print(f"Input tokens      : {totals['input_tokens']:,}")
    print(f"  cache write     : {totals['cache_creation_input_tokens']:,}")
    print(f"  cache read      : {totals['cache_read_input_tokens']:,}")
    print(f"Output tokens     : {totals['output_tokens']:,}")
    print(f"Estimated cost    : ${estimate_cost(totals):.4f}")
    print(f"Wall time         : {elapsed:.1f}s")

    if args.print_json:
        print()
        print(OUTPUT_PATH.read_text())

    return 0


if __name__ == "__main__":
    sys.exit(main())
