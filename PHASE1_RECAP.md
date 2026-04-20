# Agentic Data Analyst — Phase 1 Recap

## The plan

Build a pipeline that takes a plain-English business question about
the Olist e-commerce dataset and returns an analyst-style answer with
a trustworthy confidence label. Seven layers, each with one job:

| Layer | What it does |
|---|---|
| 0 | Skeleton + Olist data loaded into Postgres |
| 1 | Schema understander — one-time data dictionary |
| 2 | Query planner — breaks a question into 1–4 SQL-answerable sub-questions |
| 3 | SQL generator — turns each sub-question into validated, executed SQL |
| 4 | Sanity checks — rules + Haiku judgment on each result |
| 5 | Reconciliation — cross-check sibling SQL paths when the planner fans out |
| 6 | Confidence + retry + presenter — HIGH / MEDIUM / LOW / UNABLE label plus a four-section report |

## Things we hit along the way, and what we did about them

**Layer 5 didn't match what we'd actually built.**
The original BUILD_PLAN said "generate an alternative SQL," but by the
time we got there the planner was already fanning a metric out into
sibling sub-questions (e.g., revenue_from_items_and_freight vs
revenue_from_payments). Reframed Layer 5 as reconciling those
siblings rather than re-prompting the generator for another path.

**Multi-row reconciliation could silently wrong-compare.**
If you compare two multi-row tables by row position instead of by
grouping key, you can get "agreement" that's actually nonsense. Made
key-join mandatory and made key-alignment failure a hard high-severity
fail on its own, independent of how small the delta looks.

**Haiku invented a threshold.**
In one eval run the reconciliation note said "within the 0.5%
threshold" — but we'd never given it that number. Tightened the
prompt with an explicit rule: no invented thresholds or policy
references, only use the delta provided.

**Same question, two different answers across runs.**
Q3 2017 revenue came out as $2.05M on one run and $1.96M on another,
because the SQL generator wasn't stable about the order-status filter.
Logged as a Layer-7 task (#30). Not a bug to fix right now — it's a
measurement problem, and we need the eval harness in place before we
can diagnose it properly.

**Retry scope was ambiguous.**
Settled it: retries live at the SQL-generator level only, capped at 2
per sub-question, with structured feedback (severity, rule flags,
previous SQL) — not prose. Reconciliation failures do NOT trigger
retries; they lower confidence instead.

**Skipped-recon = MEDIUM felt too strict** for questions that
genuinely have a single definition (e.g., unique customer count).
Kept it MEDIUM anyway for now until Layer 7 can distinguish
"not applicable" from "planner missed a cross-val." Logged as #39.

**Sandbox couldn't remove a git lock** during the Layer 6 commit, so
we wrote the commit message to a file and you ran the commit from
your own shell.

## Where we landed

Seven commits, Layer 0 through Layer 6, end-to-end eval passes 2/2
on the north-star case:

- Q3 2017 revenue = approximately $1.96M
- Two independent paths: $1,957,760 (items+freight) vs $1,958,126 (payments)
- Delta 0.019%, confidence HIGH
- Cost ~$0.15 per happy-path question, ~$0.014 per refusal

## What's left — Layer 7

No new architecture. All measurement.

1. **Curated eval set** — 15–20 questions covering happy-path scalar,
   happy-path cross-validation, planner refusal, tricky joins, known
   gotchas, and retry-triggering cases if we can find reliable ones.
2. **N-trial harness** — run each question multiple times and
   capture: planner answerable rate, confidence-label distribution,
   reconciliation delta distribution, pass/fail rate by layer, cost
   and latency per run.
3. **Drift analysis** — especially for #30 (Q3 revenue drift). The
   harness should make the unstable choice visible in the SQL text
   distribution; likely a single filter decision.
4. **Recon-skipped reason split** — #39. Once we have eval data we'll
   know whether skipped-recon is mostly legitimate single-path or a
   planner miss, and can decide if "genuinely single-path" deserves
   HIGH.

Goal for Layer 7 in one line: turn both open tasks from guesses into
numbers.
