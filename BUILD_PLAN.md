# BUILD_PLAN.md

**Agentic Data Analyst — Layered Build Plan**

This document divides Phase 1 into 8 sequential layers. Each layer has a Claude Code prompt, a your-part checklist, and a done-when definition. Work one layer at a time. Do not open the next layer until the current one is done and reviewed.

The purpose of this layering is to keep you in the loop on the decisions that make this project *yours* (prompts, domain logic, eval design) while letting Claude Code accelerate the mechanical parts (scaffolding, plumbing, deploy config).

Companion document: `FOUNDATION.md` — scope, decisions, north-star example. Read that first if you haven't.

---

## How to use this document

1. Open only the current layer. Don't skim ahead.
2. Do the **Your Part** section first. This is where your judgment lives.
3. Paste the **Claude Code Prompt** into Claude Code.
4. Review the output against the **Done-When** checklist.
5. If something earlier is broken, go back and fix it before moving on. Don't accumulate debt.
6. Between layers, you may discover that earlier prompts need editing. Edit them. That's the whole point of separating prompt authoring from code generation.

---

## Layer 0 — Repo Setup & Data Loading

### Your Part (15 minutes)

1. Create a new GitHub repo (suggested name: `agentic-data-analyst`)
2. Drop `FOUNDATION.md` in the root
3. Sign up for Railway, create a new project, provision Postgres
4. Download the Olist dataset from Kaggle (`brazilian-ecommerce` by olistbr) and place CSVs in a local `/data/` directory you will NOT commit
5. Grab your Railway Postgres connection string and Anthropic API key, put both in a local `.env` file, add `.env` to `.gitignore` **before committing anything**

### Claude Code Prompt

```
I'm building an agentic data analysis platform. See FOUNDATION.md in
the repo for full context.

For this session, set up the project skeleton:

1. Create a Python project structure:
   /agents/        (will hold each agent module)
   /scrutiny/      (will hold the three scrutiny checks)
   /eval/          (will hold eval harness and questions)
   /prompts/       (will hold agent prompts as .md files)
   /ui/            (will hold Streamlit app)
   /data/          (will hold Olist CSVs - gitignored)
   /db/            (will hold schema + load scripts)
   main.py, requirements.txt, .gitignore, .env.example

2. requirements.txt should include: langgraph, anthropic, sqlalchemy,
   psycopg2-binary, streamlit, pandas, python-dotenv, sqlglot, pydantic.
   Pin versions to current stable.

3. Write /db/load_olist.py that:
   - Reads all 8 Olist CSVs from /data/ (skip geolocation)
   - Creates tables in Postgres with proper types (dates as timestamps,
     IDs as varchar, money as numeric)
   - Adds primary keys and foreign key constraints based on the Olist
     schema
   - Loads the CSVs via pandas.to_sql or bulk COPY
   - Verifies row counts at the end and prints them

4. Write a minimal /db/connection.py that provides a SQLAlchemy engine
   from DATABASE_URL env var, with connection pooling configured for
   a small app (pool_size=5).

5. .gitignore should include: .env, /data/, __pycache__, .venv, *.pyc

Don't build agents yet. Just the skeleton and data loading.
```

### Done-When

- Running `python db/load_olist.py` populates all 8 tables in Railway Postgres
- `SELECT COUNT(*) FROM olist_orders_dataset` returns approximately 99,441
- Repo structure matches the layout above

### Your Review Checklist

- Does `load_olist.py` load the Portuguese→English category translation as a **separate table**? (It should. Join happens at query time, not load time.)
- Are the foreign keys correct? Verify: `orders → customers`, `order_items → orders`, `order_items → products`, `payments → orders`, `reviews → orders`, `sellers → order_items`.
- Is `.env` properly gitignored? Run `git status` and confirm it doesn't appear.

---

## Layer 1 — Schema Understander

This is the agent where your judgment matters most, because a bad data dictionary poisons every downstream step. You hand-write the prompt for this one. Claude Code builds the plumbing around it.

### Your Part (1–2 hours)

Write `/prompts/schema_understander.md` yourself. This is the highest-leverage hour you'll spend on this project.

**Starter template — fill in the Olist-specific parts by actually exploring the data first.** Before you paste this into a file, connect to your Postgres, poke at 3–4 tables, sample some rows. You will discover at least 2–3 more gotchas the template doesn't have. Add them. That exploration is the work.

```markdown
# Schema Understander Prompt

You are a data dictionary generator for a Postgres database containing
Brazilian e-commerce data from Olist. Your job is to produce a
compressed, LLM-friendly data dictionary that downstream SQL-generation
agents will use to write correct queries.

## Your output format (strict JSON):

{
  "tables": {
    "<table_name>": {
      "purpose": "one-sentence description of what this table represents",
      "grain": "what one row represents (e.g., 'one line item in one order')",
      "primary_key": "<column>",
      "foreign_keys": [{"column": "...", "references": "table.column"}],
      "columns": {
        "<col_name>": {
          "type": "postgres type",
          "semantics": "what this column MEANS, not just what it stores",
          "gotchas": "anything a careless SQL writer would get wrong"
        }
      },
      "common_joins": ["typical join patterns from this table"],
      "typical_questions": ["kinds of business questions this table answers"]
    }
  },
  "cross_table_gotchas": [
    "dataset-wide warnings about reconciling across tables"
  ]
}

## Critical gotchas you MUST flag:

1. order_items.order_item_id is a SEQUENCE NUMBER within an order
   (1, 2, 3...), NOT a quantity. Multiplying price by order_item_id
   is a bug. To get revenue per order, SUM(price) grouped by order_id.

2. payments.payment_value INCLUDES freight; order_items.price does NOT.
   Computing "revenue" two ways will legitimately differ by freight total.

3. payments can have MULTIPLE ROWS per order (installment payments).
   Summing payment_value without grouping inflates totals.

4. orders.order_status values include: delivered, shipped, canceled,
   unavailable, invoiced, processing, approved, created. "Revenue"
   questions usually want to exclude canceled and unavailable.

5. products.product_category_name is in PORTUGUESE. The translation
   table product_category_name_translation maps to English, but
   THREE categories have no translation: pc_gamer,
   portateis_cozinha_e_preparadores_de_alimentos, and null.

6. reviews.review_score is 1-5 integer. Nulls exist. Don't AVG without
   filtering nulls.

7. Dates: order_purchase_timestamp is when customer ordered.
   order_delivered_customer_date is when they received.
   order_estimated_delivery_date is the promise. Delivery performance
   questions need to distinguish these three.

[ADD MORE AS YOU DISCOVER THEM]

## Process:

1. Use the provided Postgres MCP connection to explore: \d+ <table>,
   sample rows, value distributions for categorical columns.
2. Produce the JSON dictionary.
3. For any column whose semantics aren't obvious from its name,
   sample 10 rows and look at actual values before writing semantics.
4. Err on the side of OVER-DOCUMENTING gotchas. Every gotcha you
   catch here prevents a bug downstream.
```

### Claude Code Prompt

```
I'm building the Schema Understander agent. The prompt lives at
/prompts/schema_understander.md — use it verbatim as the system prompt.
Don't modify it.

Build /agents/schema_understander.py that:

1. Takes a database connection and an Anthropic client as inputs
2. Provides schema exploration tools to Claude (via tool use):
   - list_tables() -> list of table names
   - describe_table(name) -> columns with types, constraints, sample values
   - sample_rows(table, n=5) -> n sample rows as dicts
   - value_distribution(table, column, top_n=10) -> most common values
3. Calls Claude Haiku 4.5 with the system prompt from the .md file
4. Implements a multi-turn tool-use loop: Haiku explores schema via
   the tools above, then produces final JSON output
5. Validates the output matches the expected schema (use pydantic)
6. Caches the result to /db/data_dictionary.json — only regenerates
   if schema has changed (check by hashing table list + column lists)
7. Uses Anthropic's prompt caching on the system prompt

Include a simple CLI entry point: `python -m agents.schema_understander`
that regenerates the dictionary and prints it.

Token budget: this should complete in under $0.20 per full regeneration.
Log token usage at the end.

Do NOT build any other agents yet. Just this one.
```

### Done-When

- Running the CLI produces `/db/data_dictionary.json` with all 8 tables documented
- You read the output and every gotcha you put in the prompt is reflected in the dictionary
- Token cost was under $0.20

### Your Review Checklist

- Does the dictionary correctly flag `order_item_id` as a sequence (not a quantity)?
- Are the foreign keys complete?
- Did Haiku *add* any gotchas you didn't explicitly list? (Good sign if yes — means the prompt is working.)
- Are there columns where `semantics` is just "stores the X"? That's too shallow. Refine the prompt and regenerate.

---

## Layer 2 — Query Planner

### Your Part (30 minutes)

Draft `/prompts/query_planner.md`. Starter template below. **Before finalizing, add 2–3 more example input/output pairs from your own thinking about Olist questions.**

```markdown
# Query Planner Prompt

You decompose natural-language business questions about Olist
e-commerce data into structured sub-questions that a SQL generator
can answer one at a time.

## Inputs you receive:
- The user's question
- The data dictionary (cached)

## Your output (strict JSON):

{
  "restated_question": "your understanding of what the user is asking",
  "answerable": true | false,
  "unanswerable_reason": "if false, why — reference specific dataset limits",
  "sub_questions": [
    {
      "id": 1,
      "question": "specific, SQL-answerable question",
      "tables_likely": ["table1", "table2"],
      "aggregation_heavy": true | false,
      "cross_validation_candidate": true | false
    }
  ],
  "reconciliation_step": "if the question implies comparing multiple
    computations, describe what should be reconciled"
}

## Rules:

1. If the question cannot be answered from the dataset, say so. Examples:
   - "What's our CAC?" — no marketing spend data
   - "How did returns affect revenue?" — no returns data
   - Any question about periods outside 2016-2018

2. Each sub-question must be answerable by ONE SQL query. If it needs
   joins, that's fine — "one query" can be complex. But if it needs
   two separate computations and a comparison, split it.

3. Mark aggregation_heavy=true for: sums of money, counts of things,
   averages, rates. These will trigger cross-validation downstream.

4. Mark cross_validation_candidate=true when there are multiple valid
   ways to compute the same thing (e.g., revenue from order_items vs
   revenue from payments).

5. Prefer 1-3 sub-questions. More than 4 means you're overdecomposing.

## Examples:

Input: "What was Q3 2017 revenue?"
Output: 2 sub-questions (revenue from order_items, revenue from payments,
reconcile) because it's aggregation-heavy money and
cross_validation_candidate=true.

Input: "How many unique customers placed orders?"
Output: 1 sub-question, not aggregation_heavy, not cross_validation_candidate.

Input: "What's our profit margin?"
Output: answerable=false, reason="dataset has no cost data".

[ADD 2-3 MORE EXAMPLES FROM YOUR OWN THINKING]
```

### Claude Code Prompt

```
Build /agents/query_planner.py following the same pattern as
schema_understander.py:

1. Uses /prompts/query_planner.md as the system prompt (load verbatim,
   no modification)
2. Uses Claude Sonnet 4.6 (NOT Haiku — planning needs reasoning)
3. Takes (user_question, data_dictionary) as inputs
4. Returns a validated Pydantic object matching the JSON schema in
   the prompt
5. Uses prompt caching on the data dictionary (it's the biggest
   reused context)
6. Logs token usage per call

Also add a simple eval script /eval/test_planner.py that runs the
planner on these 5 test questions and prints outputs:
  1. "What was Q3 2017 revenue?"
  2. "How many unique customers placed orders?"
  3. "What's our customer acquisition cost?"
  4. "Which product category has the most complaints?"
  5. "What will revenue be next quarter?"

Expected: #3 and #5 should return answerable=false.

Do not build SQL generation or scrutiny yet.
```

### Done-When

- All 5 test questions produce valid Pydantic objects
- Questions #3 and #5 correctly return `answerable: false`
- Question #1 has `cross_validation_candidate: true` on the revenue sub-question

---

## Layer 3 — SQL Generator (Happy Path, No Scrutiny Yet)

### Your Part (30 minutes)

Draft `/prompts/sql_generator.md`. This prompt is more mechanical than the Schema Understander but critical — it's where hallucinations are born.

```markdown
# SQL Generator Prompt

You write Postgres SQL to answer ONE sub-question at a time against
the Olist database.

## Inputs:
- The sub-question
- The relevant portion of the data dictionary
- The user's restated full question (for context)

## Output format (strict JSON):

{
  "sql": "the SQL query",
  "reasoning": "1-3 sentences explaining the join path and why",
  "expected_result_shape": "one row | N rows with columns X,Y | scalar",
  "columns_used": ["table.column", ...],
  "gotchas_considered": ["which data-dictionary gotchas you actively avoided"]
}

## Rules:

1. Use only Postgres-compatible syntax.
2. Parameterize dates with explicit literals (e.g., DATE '2017-07-01'),
   never string concatenation.
3. Always exclude canceled/unavailable orders for revenue or count
   questions unless the user asks about cancellations specifically.
4. Always use explicit JOINs, never comma-joins.
5. For any aggregation, include an ORDER BY and LIMIT when the result
   is "top N" — don't return unbounded sorted results.
6. Reference gotchas_considered: if the question is aggregation-heavy,
   you MUST list which data-dictionary gotchas you accounted for.

## Gotchas that commonly bite:
- order_item_id is a sequence, not a quantity (SUM(price), not SUM(price*order_item_id))
- payments can have multiple rows per order (installments) — use SUM grouped by order_id first if needed
- product_category_name is Portuguese — JOIN to translation table for English
- review_score has nulls — filter or COALESCE

[ADD MORE BASED ON WHAT YOU LEARN]
```

### Claude Code Prompt

```
Build /agents/sql_generator.py:

1. Uses /prompts/sql_generator.md as system prompt
2. Claude Sonnet 4.6
3. Input: (sub_question: SubQuestion, data_dictionary: dict, user_context: str)
4. Output: validated Pydantic with sql, reasoning, expected_shape,
   columns_used, gotchas_considered
5. After generation, validate the SQL is parseable with sqlglot.
   If not parseable, one retry with the parse error fed back.
6. Execute the SQL against Postgres with a 30-second timeout and
   return (results_df, row_count, exec_time_ms).
7. Prompt caching on the data dictionary.

Then build /agents/presenter.py (minimal version):

1. Takes (user_question, sub_question_results: list, confidence_label: str = "UNVERIFIED")
2. Uses Sonnet 4.6
3. Prompt at /prompts/presenter.md — I'll write it separately, for now
   put a TODO placeholder that just calls the LLM with a basic
   "narrate these results like an analyst" instruction
4. Output: markdown text for the analyst report

Then build /main.py that wires it end-to-end WITHOUT scrutiny:
  user_question
    -> query_planner
    -> (for each sub_question) sql_generator
    -> presenter
    -> print markdown

Run it on "What was Q3 2017 revenue?" and show me the output.

Total token cost for this test run should be under $0.15. Log it.
```

### Done-When

- The happy path runs end-to-end on "What was Q3 2017 revenue?"
- Output is a markdown report (rough, no scrutiny yet)
- The SQL generated for the revenue question is **correct** — uses `SUM(price)`, not `SUM(price * order_item_id)`

If the SQL is wrong: **stop and fix the Schema Understander or SQL Generator prompts before moving on.** Don't paper over it with scrutiny later. Scrutiny is a safety net, not a crutch.

### Your Review Checklist

- Read the actual generated SQL. Does it exclude canceled orders? Does it use the right date filtering?
- Read the `gotchas_considered` field. Is the SQL Generator actually engaging with the data dictionary, or ignoring the gotchas section?

---

## Layer 4 — Sanity Checks

### Your Part (20 minutes)

Draft `/prompts/sanity_check.md` describing what sanity checks should flag. Key categories:

- Empty results when non-empty expected
- Negative numbers where positive expected
- Out-of-range values (review scores outside 1–5, percentages outside 0–100)
- Suspiciously round numbers
- Excessive nulls

### Claude Code Prompt

```
Build /scrutiny/sanity.py:

1. A mix of rule-based Python checks and one Haiku call for
   judgment-based checks
2. Input: (sql_result_df, sub_question: SubQuestion, expected_shape: str)
3. Output: SanityResult with {passed: bool, flags: list[str], severity: str}
4. Rule-based checks (no LLM needed):
   - Empty result when sub_question implies non-empty
   - Row count vs expected_shape mismatch
   - Numeric columns: any negative where positive expected (based on
     column name heuristics: revenue, price, count, etc. should be >0)
   - Null rate > 50% in any column
5. Haiku check (one call): given the sub_question and a summary of
   the result (shape + first 5 rows + aggregates), does the result
   look plausible for the question? Prompt at /prompts/sanity_check.md.

Wire sanity.py into main.py AFTER sql_generator, BEFORE presenter.
If sanity fails, for now just log it — don't retry yet. Retry logic
comes in Layer 6.
```

### Done-When

- Sanity checks run on the happy-path query and log their output
- At least one rule-based check demonstrably fires when you feed it a deliberately bad result (test this manually)

---

## Layer 5 — Cross-Validation

This is the demo-critical component. Spend time here.

### Your Part (30 minutes)

Draft `/prompts/cross_validator.md` describing how to generate an alternative SQL and compare results. The key instruction: the alternative must use a **different join path or base table**, not just a rephrased version of the same query.

### Claude Code Prompt

```
Build /scrutiny/cross_validation.py:

1. Triggered only when sub_question.cross_validation_candidate == true
2. Steps:
   a. Call SQL Generator AGAIN with a modified prompt that says
      "produce an ALTERNATIVE SQL that answers the same sub-question
      using a DIFFERENT join path or base table"
   b. Execute the alternative SQL
   c. Compare results using tolerance logic:
      - Scalar numeric: compute delta %, pass if < 2%
      - Multi-row: join on grouping key, check per-row delta
   d. If mismatch, use Haiku to produce a reconciliation note
      ("the difference of X% is likely explained by Y")
3. Output: CrossValidationResult with {passed, v1_result, v2_result,
   delta_pct, reconciliation_note}

Wire into main.py AFTER sanity. Same rule for now: log but don't retry.
```

### Done-When

- On the revenue question, cross-validation generates a second SQL (using a different join path), runs it, and reports the delta
- The reconciliation note for the Q3 revenue case mentions freight as the explanation

---

## Layer 6 — Confidence Scoring & Failure Handling

### Your Part (10 minutes)

No new prompt to draft, but finalize `/prompts/presenter.md` at this layer. Use the north-star example in `FOUNDATION.md` Section 4 as the output template.

### Claude Code Prompt

```
Add /scrutiny/confidence.py:

1. Derives confidence label from (sanity_result, cross_val_result, retry_count)
2. Labels: HIGH, MEDIUM, LOW, UNABLE
3. Rules per FOUNDATION.md Section 2.5

Then update main.py to add retry logic:

1. If sanity fails OR cross_validation fails OR both, retry SQL generation
   with failure reason included in the prompt
2. Max 2 retries per sub-question
3. After 2 failed retries, the sub-question gets confidence=UNABLE and
   the presenter is told to acknowledge this

Finalize /prompts/presenter.md to narrate the full analyst report with
all four sections: Answer+Confidence, Methodology, Verification, Caveats.
Use the north-star example in FOUNDATION.md Section 4 as the template
for output format.
```

### Done-When

- The full pipeline, with retries, runs on the Q3 revenue question and produces a four-section analyst report
- Confidence label is HIGH (because cross-validation passed)
- Running on a deliberately malformed question produces confidence=UNABLE after retries are exhausted

---

## Layer 7 — Eval Harness

### Your Part (1–2 hours)

Write 10 eval questions by hand in `/eval/questions.jsonl`. Each line is a JSON object. Mandatory composition:

- Include the north-star example ("What was Q3 2017 revenue...")
- At least 2 unanswerable questions (CAC, future periods, returns data)
- At least 2 aggregation-heavy questions that should trigger cross-validation
- At least 1 tricky multi-table join
- At least 1 question where sanity checks should fire (feed in an obviously bad framing)

Format each line as:
```json
{"question": "...", "expected_answer": "...", "expected_confidence": "HIGH|MEDIUM|LOW|UNABLE", "expected_scrutiny_trigger": "sanity|cross_validation|both|none"}
```

### Claude Code Prompt

```
Build /eval/runner.py:

1. Loads /eval/questions.jsonl (each line: {question, expected_answer,
   expected_confidence, expected_scrutiny_trigger})
2. For each question, runs main.py pipeline and captures: answer text,
   confidence label, token cost, which scrutiny checks fired,
   wall-clock time
3. Produces /eval/results/<timestamp>.json with all 5 metrics per
   FOUNDATION.md Section 5
4. Prints a summary table at the end

Run it on the 10 seed questions, show me the output.
```

### Done-When

- Eval runs on all 10 questions
- Summary table shows all 5 metrics
- P50 cost per query is under $0.15
- Uncertainty flagging accuracy on the 2 unanswerable questions is 100% (they should correctly return UNABLE)

---

## Layer 8 — Streamlit UI & Deploy

### Your Part (15 minutes)

No prompt drafting here, but before Claude Code touches the UI, sketch on paper how you want the four sections to lay out. Specifically: which section expands/collapses by default, and in what order the progressive rendering appears.

### Claude Code Prompt

```
Build /ui/app.py as a Streamlit app implementing the analyst-report UI
from FOUNDATION.md Section 2.6:

1. Text input at top for user question
2. On submit, runs the pipeline from main.py with progressive rendering:
   - "Decomposing question..." appears immediately
   - Sub-questions appear as planner completes
   - SQL queries appear as generator produces them
   - Scrutiny outcomes appear as checks run
   - Final answer + confidence appear last
3. Use st.status and st.expander for collapsible sections
4. Output layout: Answer+Confidence, Methodology, Verification, Caveats
   (the four sections)

Then update the Railway deployment:
1. Procfile or railway.json running streamlit on the Railway-provided PORT
2. Environment variables documented in .env.example

Test that the deployed URL works. Give me the URL.
```

### Done-When

- Streamlit app runs locally against the Railway Postgres
- Deployed to Railway, accessible at a public URL
- Progressive rendering works — sections appear as the pipeline produces them, not all at once
- The Q3 revenue question produces a full analyst report in the UI

---

## After Layer 8

Phase 1 is done when:

1. All 8 layers are complete per their done-when definitions
2. You grow the eval set from 10 to 50+ questions
3. Run the full eval, record final metrics
4. Write the blog post using the outline in `FOUNDATION.md` Section 6, filling in the measured numbers
5. Record a 3–5 minute demo video showing the north-star example running end-to-end

Only then does Phase 2 start.

---

## Meta-Rules for Working with Claude Code

1. **Don't paste multiple layers at once.** One layer per session.
2. **Read every line of generated code before accepting it.** If you don't understand something, ask Claude Code to explain before moving on.
3. **If a layer reveals a flaw in an earlier layer, go back and fix it.** Don't accumulate debt.
4. **Prompts are yours, not Claude Code's.** Every `/prompts/*.md` file should reflect your thinking about the dataset and failure modes. Claude Code can *suggest* prompt improvements, but you are the final author.
5. **Cost-check after every layer.** If token usage is trending above target, pause and investigate before adding more layers.
6. **Commit after every layer passes its done-when.** Use clear commit messages: `feat(layer-2): query planner with cross-validation flagging`. This gives you easy rollback points if a later layer reveals problems.

The goal is not "have Claude Code build the project." It is "have Claude Code accelerate the mechanical work while you stay in the loop on the parts that make this project *yours*."
