# Agentic Data Analyst

A multi-layer LLM pipeline that answers business questions against a relational database by writing SQL, verifying its own answers, and reporting a calibrated confidence label. Built on the Olist Brazilian e-commerce dataset (9 tables, ~100K orders).

Given a natural-language question, the system routes it through six layers — planner, SQL generator, sanity checker, reconciler, confidence deriver, presenter — and emits a four-section analyst report labeled `HIGH`, `MEDIUM`, `LOW`, or `UNABLE`. The label is derived deterministically from structured signals (did the SQL execute, did sanity rules pass, did two independent SQL paths agree); it is **not** an LLM grading another LLM.

---

## What it does

```
$ python -m main "What was Q3 2017 revenue?"

Confidence: HIGH
Reconciliation: items+freight vs payments agreed within 0.02%
Sanity: all rules passed
Cost: $0.16   Wall time: 28.4s

Q3 2017 revenue was approximately $1.90M. Two independent calculations
(items+freight sum vs payment-value sum) reconciled to within 0.02% on
the same filter ruleset, and both passed null-rate and rate-range
sanity checks.
```

For questions the dataset cannot support (e.g., *"What is customer acquisition cost?"* — no marketing spend data), the planner refuses and returns `UNABLE` rather than fabricating a proxy answer.

---

## Architecture

Six layers, each with a narrow responsibility:

| Layer | Model | Responsibility |
|-------|-------|----------------|
| 2. Planner | Sonnet 4.6 | Decompose the question into one or more sub-questions. Decide answerability. Fan out cross-validatable metrics into sibling SQL paths. |
| 3. SQL Generator | Sonnet 4.6 | Per sub-question, generate SQL, execute it, return a DataFrame. Retries on execution errors with structured error context. |
| 4. Sanity | Haiku 4.5 + rules | Deterministic DataFrame rules (negative monetary, null-rate, rate-range, etc.) plus Haiku judgment on result shape. A failed sanity check triggers one regenerate+re-execute up to `MAX_RETRIES`. |
| 5. Reconciliation | Haiku 4.5 + rules | When the planner fanned out, compare sibling results deterministically (symmetric percentage delta on scalars; outer-merged per-row delta on grouped results). Haiku writes a one-sentence explanation, never decides pass/fail. |
| 6. Confidence | No LLM | Derive `HIGH / MEDIUM / LOW / UNABLE` from the preceding layers' structured outputs. Pure function. |
| 6. Presenter | Sonnet 4.6 | Narrate a four-section report (answer, method, caveats, sources) grounded in the full audit trail. |

Retries live at the generator level only. Reconciliation failures **lower confidence** rather than trigger a retry — the system is honest about disagreement instead of hiding it.

---

## Results

Latest N=3 run across 15 curated questions (5 straightforward, 7 tricky, 3 unanswerable):

- **45 / 45** trial label hits against expected confidence bands.
- **0** crashes across 45 trials.
- **~$1.60** per full N=3 run (\$0.036 per trial average).
- **15 minutes** wall time for the full run.
- **\$0.17** to re-run a single question in isolation (for focused debugging).

Progression over the last ~10 commits on this branch:

| Commit | Scoreboard | Notable change |
|--------|------------|----------------|
| Baseline (pre-Q8 fix) | 40 / 45 | Q8 pinned to outdated `[MEDIUM, LOW]` expectation. |
| `7f9ec90` | 43 / 45 | Q8 expected updated to reflect generator improvement (uses `customer_unique_id` correctly). |
| `254de18` | 43 / 45 | Q12 reconciler gains a cross-sibling common-column heuristic; planner prompt adds grain-alignment rules. Q12 trial 3 lands HIGH. |
| `c1f2b6d` | 45 / 45 | Q12 expected admits `LOW` — the reconciler correctly flags a genuine semantic ambiguity (per-payment_type items+freight has no natural decomposition). |

Every commit message carries the N=3 evidence that justified it. The eval harness is the system's own observability.

---

## What this project demonstrates

- **Designing multi-stage LLM pipelines** with narrow per-layer responsibilities and structured hand-offs between them.
- **Deterministic verification of LLM output.** The confidence layer derives its label from typed signals — SQL success, sanity rule outcomes, reconciliation delta, planner answerability — rather than asking another LLM "are you sure?".
- **Cross-validation via planner fan-out.** When a metric has multiple legitimate definitions (revenue-from-items-and-freight vs revenue-from-payments), the planner emits both as sibling sub-questions and the reconciler compares them. Disagreement surfaces as lower confidence, not as a wrong answer.
- **Building eval harnesses for LLM systems.** N-trial runner with per-trial error isolation, cost tracking, wall-time measurement, drift analysis over SQL text, and `--n-trials` / `--question-ids` flags for cheap focused debugging.
- **Handling real failure modes.** SQL execution errors with structured retry context, metric-definition ambiguity, key-alignment failures in multi-row reconciliation, generator drift on `customer_id` vs `customer_unique_id`, shape-mismatch in cross-validation.
- **Measure-first engineering discipline.** Every behavioral change lands with a commit that carries the N=3 scoreboard before and after. Fixes that sound good but don't move the numbers don't ship.

---

## Design choices worth calling out

**Deterministic confidence, not LLM-as-judge.** Asking an LLM to rate its own answer correlates with output length and fluency more than with correctness. The confidence layer here is a pure function of the structured signals already captured by earlier layers. It can be unit-tested; the thresholds can be tuned; the label is reproducible.

**Reconciler operates on DataFrames, not text.** Sibling results are compared by symmetric percentage delta on scalars and by outer-merged per-row delta on grouped results. Key-alignment failures are severity="high" on their own, independent of delta magnitude. The LLM's only role is writing the one-sentence narration; it cannot override the deterministic verdict.

**Planner skip taxonomy.** Not every plan needs reconciliation. The planner distinguishes `single_sub_question` (legitimately one SQL path), `complementary_views` (two sub-questions that slice one population differently — not a cross-validation), and `insufficient_siblings` (planner wanted reconciliation but an upstream sub-question failed). Each category maps to a specific confidence treatment downstream.

**Adversarial test coverage for the reconciler.** 14 rule-based cases in `eval/test_reconciliation.py` covering key misalignment, shape mismatch, ambiguous inference, and the recent cross-sibling common-column heuristic — all running without an API call so they're free to run in CI.

**Honest refusal on unanswerable questions.** Planner returns `UNABLE` with a reason for questions the dataset cannot support (CAC without marketing spend, profit margin without COGS, future revenue). The pipeline short-circuits — no SQL is generated, no confidence is fabricated.

---

## Running it

Prerequisites:
- Python 3.10+
- PostgreSQL with the Olist dataset loaded (see `db/load_olist.py`)
- `ANTHROPIC_API_KEY` in `.env`

```bash
pip install -r requirements.txt
python db/load_olist.py       # one-time: loads Olist CSVs into Postgres
python -m main "What was Q3 2017 revenue?"
```

Full eval harness:

```bash
python -m eval.runner                                   # N=3 across all 15 questions (~$1.60, 15 min)
python -m eval.runner --n-trials 1                      # fast smoke, ~$0.55
python -m eval.runner --n-trials 1 --question-ids 12    # single-question debug, ~$0.17
```

Results are written to `eval/results/<timestamp>.json` with per-trial SQL text, sanity outcomes, reconciliation deltas, confidence labels, and per-layer token usage.

---

## Repo layout

```
agents/            Layer 2, 3, 6 (planner, sql_generator, presenter)
scrutiny/          Layer 4, 5, 6 (sanity, reconciliation, confidence)
prompts/           System prompts for each LLM-backed layer
db/                Postgres connection + Olist loader
eval/
  questions.jsonl  15 curated questions with expected confidence bands
  runner.py        N-trial harness
  results/         Timestamped N=3 JSON blobs
  test_*.py        Adversarial rule-based tests for each layer
main.py            End-to-end orchestrator + CLI entry point
```

---

## Honest limitations

- **Olist only.** The system has been developed and measured against one dataset. The claim of domain reuse is untested until Phase 2 runs it on a genuinely different schema.
- **CLI only.** No UI has been built. A Streamlit progressive-render UI is planned but not part of Phase 1.
- **One sanity rule deferred.** A sanity check for `customer_id` vs `customer_unique_id` drift is in the backlog (ticket #46), deliberately unimplemented until a generator regression gives it something real to catch.
- **Per-payment_type items+freight is semantically ambiguous.** Q12's reconciler correctly flags that `revenue_from_items_and_freight` has no natural decomposition by payment method; a deeper architectural fix (planner rule for grain-compatible fan-out, or SQL rule for proportional allocation) is documented but deferred.

---

## Acknowledgments

Dataset: [Olist Brazilian E-Commerce](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) (public, CC BY-NC-SA).
Built with Anthropic's Claude Sonnet 4.6 and Haiku 4.5.
