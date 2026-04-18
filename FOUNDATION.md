# FOUNDATION.md

**Agentic Data Analyst — Project Foundation**

A modular agentic platform that analyzes data like a senior analyst, with a multi-stage verification pipeline that catches hallucinated metrics before they reach the user.

This document is the single source of truth for Phase 1 scope, locked decisions, and the north-star example every agent must be able to handle. It supersedes any conflicting earlier planning.

---

## 1. The Pitch (Internal)

> A modular agentic platform that analyzes data like a senior analyst, with a multi-stage verification pipeline that reduced hallucinated metrics from X% to Y% on a 75-question eval set. The core is domain-agnostic; I've shipped it for product analytics, and adding new domains takes days, not weeks, because the plugin architecture was grounded in real implementations, not speculation.

X and Y are placeholder targets for numbers the Phase 1 eval run produces. Do not publish until measured.

---

## 2. Locked Decisions

Six foundation decisions are closed. Changing them mid-build requires a deliberate rescope, not a drift.

### 2.1 Pacing: Milestone-driven, no calendar deadline

Phases advance when the "done" definition is met, not when the clock says so.

Phase 1 is done when all of the following are true:
- Full pipeline (Schema → Planner → SQL Gen → Scrutiny → Presenter) runs end-to-end on Olist
- Eval set of 50–100 Q/A pairs exists, with at least three categories: straightforward, tricky, and unanswerable
- Cost per query is measured and stays under P50 < $0.15, P95 < $0.50 across the eval set
- Analyst-report UI renders progressively on a deployed public URL
- Blog post outline exists with placeholder numbers ready to be filled in from the eval run

Guard against infinite polish: once all five bullets are true, Phase 2 starts. No "one more feature" loop.

### 2.2 Dataset: Olist Brazilian E-Commerce

Loaded into Postgres, 8 of 9 tables (skip `geolocation` for v1).

Why Olist:
- Real relational schema forces real SQL; joins across 4–5 tables are required for meaningful questions. This is what gives the scrutiny layer something real to catch.
- Real semantic columns (`product_category_name`, `review_score`, `delivery_date`, `payment_type`) mean the Schema Understander has something to actually reason about. RetailRocket's hashed IDs offered none of this.
- Natural data quality issues (missing category translations, review/delivery date misalignments, null product categories) are the natural environment for sanity checks to fire.

Phase 2 will use a genuinely different dataset (candidates: Superstore sales, a SaaS-style MRR/ARR synthetic set) to actually stress-test the "80% reusable / 20% domain-specific" claim rather than reusing Olist in a different costume.

### 2.3 Hosting: Railway, single provider

Two services inside one Railway project:
1. Managed Postgres (Olist data loaded, ~500 MB)
2. One combined app service running Streamlit + LangGraph + SQLAlchemy in a single Python process

Public URL: Railway's auto-provisioned domain, or a custom domain on top.

Budget: $5–15/month for hosting. LLM API costs will dwarf this during development — don't over-optimize hosting.

Why Railway over alternatives:
- Railway's Hobby plan doesn't sleep; a recruiter clicking the demo link will not hit a 45-second cold start. Render's free tier sleeps after 15 minutes of inactivity, which is disqualifying for a portfolio demo.
- Single-provider simplicity matters more than theoretically-optimal infrastructure for a solo build. Every extra dashboard is a drag on momentum.
- Fly.io is a valid alternative with comparable managed Postgres and often better per-dollar compute, but requires Docker and `fly.toml` configuration that costs more setup time than it saves.
- Vercel + Supabase is wrong for this project. Vercel is optimized for frontend-heavy apps with serverless functions; this app is backend-heavy with stateful LangGraph orchestration and per-query SQL execution.

Operational requirements:
- Nightly Postgres dump exported to external storage (S3, R2, or similar).
- Demo video linked in README as a fallback if the hosted demo is ever down.

### 2.4 Per-Query Cost Budget

The 5th eval metric, enforced at architecture level, not just logged.

Targets:
- **P50 < $0.15**
- **P95 < $0.50**

Model allocation:
- **Sonnet 4.6** ($3 / $15 per MTok): planning, SQL generation, presenter narration
- **Haiku 4.5** ($1 / $5 per MTok): schema reasoning, sanity validation, cross-validation comparison, self-critique (if added later)

Cost-control mechanisms (non-negotiable):

1. **Prompt caching on schema.** The data dictionary is 2–5K tokens and is reused on every query. Cache reads cost 10% of standard input. Without caching, the schema line item alone would eat the P50 budget on questions with multi-step planning.
2. **Retry ceiling.** Max 2 retries on failed scrutiny. After two failures, the system returns "I can't answer this reliably because X" — which is explicitly an intended behavior, not a failure mode.
3. **Conditional cross-validation.** Cross-validation fires on ~20–30% of queries, not all of them. Triggers: aggregation-heavy question, sanity flagged anomaly, or confidence score below threshold.
4. **Bounded EXPLAIN ANALYZE critique.** Max 1 rewrite per SQL query, triggered only when the plan shows a sequential scan on a large table or a cartesian product.
5. **Haiku for validation, Sonnet for reasoning.** Self-critique (if ever added) is a Haiku call. Validation agents never use Sonnet.

Tracking: every API call tagged by pipeline stage (schema/plan/gen/validate/critique/present), tokens logged per tag, rolled up per query, aggregated across eval set. The interesting derivative metrics for the blog post are **cost per correctly flagged uncertainty** and **cost per caught hallucination** — these are the economic story, not absolute cost.

### 2.5 Phase 1 Scrutiny Scope

Three checks ship in Phase 1, plus failure handling as infrastructure. Three others are deferred.

**Shipping in Phase 1:**

- **Sanity checks** — rule-based assertions running after every SQL query: non-empty result when expected, numeric ranges (review scores 1–5, prices positive), null rate below threshold, row count plausibility vs schema statistics. Mostly Python, occasional Haiku call for judgment-based range checks.
- **Cross-validation** — LLM-generated second independent SQL query using a different join path or base table, with reconciliation logic to compare results. Within tolerance = pass. Out of tolerance = retry or flag. This is the single most demo-able component of the entire system.
- **Confidence scoring** — every numeric output in the presenter carries a label derived from check outcomes. Labels: HIGH (sanity passed, cross-validation matched), MEDIUM (sanity passed, cross-validation skipped or within loose tolerance), LOW (sanity flagged, or cross-validation mismatch within retry budget), UNABLE (retries exhausted — system declines to answer).

**Supporting infrastructure:**

- **Failure handling** — LangGraph retry plumbing that routes sanity/cross-validation failures back to the SQL generator with the failure reason included in the retry prompt.

**Deferred to Phase 2 or later:**

- **Self-critique** — overlaps with what sanity + cross-validation already catch. Revisit only if the Phase 1 eval shows a category of failures the first three miss.
- **Statistical checks** — Simpson's paradox detection, weighted aggregations, sample size enforcement. High build effort, low firing frequency. Add only when there's a specific eval failure to test against.

Implication for eval set design: the 50–100 Q/A pairs must include questions where sanity fails, questions where cross-validation catches a bug, and questions where the honest answer is "I can't answer this reliably." If the eval set doesn't exercise each of the three shipped checks, the blog post's numbers mean nothing.

### 2.6 UI Paradigm: Analyst Report

Structured multi-section output per query. Reasoning is exposed by default, not hidden behind toggles.

Section layout:

1. **Answer + Confidence label** — the short version, on top
2. **Methodology** — sub-questions the planner generated, SQL for each, expandable
3. **Verification** — which scrutiny checks ran, outcomes, any reconciliation notes
4. **Caveats** — data limitations, assumptions, anything the user should know before acting on the answer

Rendering: progressive, not all-at-once. Sections appear as the pipeline produces them. Latency becomes a feature — the user watches the system think.

Follow-up conversation threading is explicitly out of scope for v1. One question = one report. Follow-ups are Phase 2.

Tech: Streamlit for v1. Next.js migration deferred until there is a specific UX problem Streamlit cannot solve. If the Streamlit version is good enough for the demo video, skip the migration entirely.

Why not chat: chat hides the differentiator. Scrutiny work collapsed behind a "show reasoning" toggle means most viewers never see it. The demo looks like every other LLM demo.

Why not dashboard: wrong shape. Dashboards are for known metrics; this project's value is answering novel ad-hoc questions with verification.

---

## 3. Architecture (Recap with Updates)

```
User Question
    ↓
[1] Schema Understander (Haiku, cached)    →  Data Dictionary
    ↓
[2] Query Planner (Sonnet)                 →  Sub-questions
    ↓
[3] SQL Generator + Optimizer (Sonnet)     →  SQL + EXPLAIN ANALYZE
    ↓ (bounded retries)
[4] Scrutiny Layer:
      a. Sanity checks (Python + Haiku)
      b. Cross-validation (Sonnet, conditional)
      c. Confidence scoring (derived from a+b)
    ↓ (retry on failure, max 2)
[5] Presenter (Sonnet)                     →  Analyst Report
```

Failure handling routes failures from step 4 back to step 3 with the reason included. After 2 retries, step 5 produces an UNABLE response with a clear explanation.

---

## 4. North-Star Example

Every agent, prompt, and check in Phase 1 must be able to handle this example correctly. If it can't, the pipeline isn't done.

**User question:** *"What was total revenue in Q3 2017, and why might it be different if I calculated it a different way?"*

### Pipeline trace

**Schema Understander** (cache hit after first run):
Identifies three tables relevant to revenue: `olist_order_items_dataset` (price × quantity per line item), `olist_order_payments_dataset` (payment.value, which includes freight and may be split across installments), `olist_orders_dataset` (for filtering by order_purchase_timestamp).

**Query Planner** decomposes into:
1. Total revenue Q3 2017, computed from `order_items`
2. Total revenue Q3 2017, computed from `payments`
3. Reconciliation: what accounts for any difference?

**SQL Generator** produces:

```sql
-- v1: from order_items
SELECT SUM(oi.price * oi.order_item_id) AS revenue_items
FROM olist_order_items_dataset oi
JOIN olist_orders_dataset o ON o.order_id = oi.order_id
WHERE o.order_purchase_timestamp >= '2017-07-01'
  AND o.order_purchase_timestamp <  '2017-10-01'
  AND o.order_status NOT IN ('canceled', 'unavailable');
```

**Scrutiny — Sanity check:** Result is $487,231. Positive, non-zero, within plausible Olist monthly-revenue range. PASS.

**Scrutiny — Cross-validation triggered** (aggregation-heavy question). SQL Generator produces v2:

```sql
-- v2: from payments
SELECT SUM(op.payment_value) AS revenue_payments
FROM olist_order_payments_dataset op
JOIN olist_orders_dataset o ON o.order_id = op.order_id
WHERE o.order_purchase_timestamp >= '2017-07-01'
  AND o.order_purchase_timestamp <  '2017-10-01'
  AND o.order_status NOT IN ('canceled', 'unavailable');
```

Result: $491,847. Delta vs v1: 0.9%.

**Reconciliation logic:** Delta is under 2% tolerance and matches the expected pattern for payment-vs-order-item reconciliation (freight included in payments but not in `price × quantity`). PASS with a reconciliation note.

**Wait — the v1 SQL above is wrong.** `order_item_id` is a line-item sequence number, not a quantity. Multiplying price by it produces nonsense. The Schema Understander's data dictionary must flag that `order_item_id` is a sequence, not a quantity, so the SQL Generator picks `COUNT(*)` or `SUM(price)` grouped correctly. This is exactly the kind of error Cross-validation catches — v1 would produce ~$2.1M (wildly wrong), v2 produces ~$490K (correct). Mismatch flagged, retry triggered.

**On retry**, SQL Generator produces:

```sql
-- v1 retry: price per line, summed
SELECT SUM(oi.price) AS revenue_items
FROM olist_order_items_dataset oi
JOIN olist_orders_dataset o ON o.order_id = oi.order_id
WHERE o.order_purchase_timestamp >= '2017-07-01'
  AND o.order_purchase_timestamp <  '2017-10-01'
  AND o.order_status NOT IN ('canceled', 'unavailable');
```

Result now ~$487K. Cross-validation delta vs v2: ~0.9% (freight-explained). PASS.

**Confidence scoring:** HIGH (sanity passed, cross-validation matched within tolerance after one retry).

**Presenter output:**

```
REVENUE ANALYSIS — Q3 2017
──────────────────────────

Answer: Q3 2017 revenue was approximately $487,000, with a
0.9% gap depending on whether you count product revenue only
(order_items) or total customer payments (which include freight
and payment-method installment artifacts).
Confidence: HIGH

Methodology
───────────
I decomposed this into 3 sub-questions:
  1. Revenue from order_items (product-side) [SQL →]
  2. Revenue from payments (customer-side)   [SQL →]
  3. Reconcile the difference

Verification
────────────
✓ Sanity: both queries returned positive values in plausible range
✓ Cross-validation: two independent queries, 0.9% delta
  - order_items (SUM of price): $487,231
  - payments (SUM of payment_value): $491,847
  - Gap explained: payments include freight; order_items do not
⚠ First SQL attempt multiplied price by order_item_id (a sequence
  number, not quantity). Caught by cross-validation, auto-retried.

Caveats
───────
⚠ Canceled and unavailable orders excluded
⚠ Returns/refunds data not present in this dataset
⚠ Installment payments counted in full at purchase timestamp
```

### What this example proves

- Schema Understander can identify semantically similar but distinct columns (`order_item_id` as sequence vs quantity).
- Query Planner decomposes into sub-questions including a reconciliation step.
- SQL Generator produces syntactically valid SQL (even if semantically wrong on first pass).
- Cross-validation catches the semantic error the LLM made on v1.
- Failure handling routes the failure back with a reason.
- Confidence scoring reflects the retry in the label.
- Presenter narrates the whole story including the caveat about the first attempt.

If the pipeline can handle this, it can handle most of the eval set. This is the build target.

---

## 5. Eval Set Design

Target: 50–100 Q/A pairs. Mandatory composition:

- **~40% straightforward** — direct questions with clean single-query answers. Baseline for correctness metric.
- **~40% tricky** — multi-table joins, ambiguous phrasing, cross-validation-relevant. Where scrutiny earns its keep.
- **~20% unanswerable** — questions the dataset cannot answer (returns data not present, questions about future periods, questions about missing categories). Where confidence scoring earns its keep by returning UNABLE.

Each Q/A pair records:
- The question (natural language)
- Expected answer or expected "UNABLE because X"
- Which scrutiny check is expected to fire (if any)
- Expected confidence label

Per-run tracked metrics:
1. **Answer correctness** (% of straightforward and tricky questions answered correctly)
2. **Uncertainty flagging accuracy** (% of unanswerable questions correctly returning UNABLE)
3. **Cost per query** (P50, P95)
4. **Scrutiny catches** (count of cases where a check caught a real bug — i.e., retry fixed the answer)
5. **Hallucination rate** (% of straightforward/tricky questions that returned a confident-wrong answer)

Metric 5 is the money number for the blog post.

---

## 6. Blog Post Outline

The blog post is not optional. It is the single highest-leverage deliverable. Writing the outline *before* coding forces the eval metrics to be concrete and the scrutiny layer's contribution to be provable.

**Working title:** *How I reduced hallucinated metrics from X% to Y% with a three-stage verification pipeline*

**Section outline:**

1. **The problem** — every LLM-on-SQL demo confidently returns wrong numbers. Concrete example of a "reasonable-looking" hallucination (use the `order_item_id` bug from the north-star example).
2. **The architecture** — five agents, with the scrutiny layer as the differentiator. Diagram.
3. **What "scrutiny" actually means** — walk through each of the three Phase 1 checks with a real example of it firing.
4. **The north-star example end-to-end** — full pipeline trace from Section 4 above.
5. **Results** — eval numbers: correctness before/after scrutiny, uncertainty flagging accuracy, cost per query, cost per caught hallucination.
6. **What this didn't solve** — honest list of failure modes, deferred checks, dataset-specific quirks.
7. **What I'd build next** — Phase 2 preview: second domain, plugin architecture.

Write the outline structurally now. Fill in measured numbers only after the Phase 1 eval run.

---

## 7. What's Explicitly Out of Scope (Anti-Goals)

Carried forward from the original plan, still binding:

- Building a modular platform on day 1 (premature abstraction trap)
- Supporting multiple domains shallowly
- Chat-with-my-PDF clone
- Skipping evals
- Skipping the writeup
- Training custom models
- Self-critique in Phase 1 (deferred pending eval gap evidence)
- Statistical checks in Phase 1 (deferred same)
- Conversational follow-ups in v1 UI
- Next.js migration until Streamlit proves insufficient

---

## 8. Open Items (No Longer Blocking)

These were flagged as open in the original plan. All are now closed:

- ~~Specific public dataset for Phase 1~~ → Olist
- ~~Hosting for live demo~~ → Railway
- ~~Weekly time budget / 6-week or 12-week pacing~~ → milestone-driven, no calendar

No remaining blockers for Phase 1 start.

---

## 9. Phase 1 First-Week Sequence

Concrete order of operations to avoid spinning wheels:

1. Spin up Railway project, provision Postgres, load Olist CSVs (all 8 tables, skip geolocation). Verify with `SELECT COUNT(*) FROM olist_orders_dataset`. Should be ~100k.
2. Build Schema Understander first, standalone. Output: a data dictionary JSON for the 8 Olist tables with column semantics, PK/FK relationships, and known gotchas (including the `order_item_id` sequence-not-quantity note). Verify by running it and reading the output.
3. Write 10 eval questions by hand, covering all three categories. Include the north-star example. This is the initial eval set; grow to 50+ during development.
4. Build the "happy path" end-to-end: Planner → SQL Gen → Presenter, no scrutiny yet, on the 10 eval questions. Get the skeleton running before adding the interesting parts.
5. Layer in Sanity checks. Re-run eval. Measure correctness delta.
6. Layer in Cross-validation. Re-run eval. Measure correctness delta.
7. Layer in Confidence scoring. Re-run eval. Measure uncertainty flagging accuracy.
8. Grow eval set to 50–100 pairs. Run full eval. Record all 5 metrics.
9. Build analyst-report UI in Streamlit with progressive render.
10. Deploy to Railway, verify public URL works, record demo video.
11. Write blog post, filling in measured numbers.

Phase 1 is done when step 11 is done.

---

*This document is the base. Any drift from these decisions during build requires a deliberate, documented rescope — not silent scope creep.*
