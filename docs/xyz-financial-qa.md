# XYZ Ltd — synthetic 10-year ledger and grounded financial Q&A

**XYZ Ltd is imaginary.** All transactions are synthetic and generated deterministically.

## Dataset

| Item | Value |
|---|---|
| Period | FY2017–FY2026 (Indian fiscal year: 1 Apr 2016 – 31 Mar 2026), 120 months, 3,652 days |
| Volume | 189,897 journal entries, 566,561 double-entry lines (every entry balances) |
| Business | technology company: Enterprise Software, Cloud Services, Hardware Solutions; 48 customers; 15 vendors; North/South/East/West/Export |
| Daily activity | sales invoices with cost of sales, customer receipts (DSO-driven lag), weekly inventory purchases, vendor payments, travel, repairs |
| Periodic activity | payroll, rent, utilities, marketing (festival-season spikes), IT, professional fees, depreciation, interest, interest income, quarterly capex, loan instalments, advance tax, annual dividends |
| Realism | ~12% revenue growth, Oct–Nov and March seasonality, a COVID-19 dip (Apr–Sep 2020), a ₹20 crore liquidity loan (May 2020), major capex in FY2019 and FY2022 |
| Scale | revenue ₹140 crore (FY2017) → ₹403 crore (FY2026); net margin 11–15% |
| Checks | balanced entries, cash always positive, assets = liabilities + equity at every year end (unit-tested) |

Code: `backend/finguard/xyz/generator.py` (seeded). Storage: `fin_accounts`, `fin_journal_entries`, `fin_journal_lines` (migration 0003).

## How FinanceBench and TAT-QA were used

They supply **question styles**, not data about XYZ:
- FinanceBench metric definitions (DPO, DSO, inventory turnover, ROA, capex %, operating cash flow ratio, multi-year averages) shaped the metric set and the conventions (e.g. average balances).
- 27 real FinanceBench questions whose metric the ledger supports were rewritten for XYZ Ltd (company replaced, years mapped into FY2017–FY2026, USD replaced by INR). They are a held-out test set.
- 250 real TAT-QA arithmetic question frames ("What is the change in … from 2018 to 2019?") were rewritten with XYZ line items as another held-out test set.
- 150 original FinanceBench questions about real companies test that the agent **refuses** anything that isn't XYZ Ltd.

## "Training" — what it is and isn't

No language model was fine-tuned. A fine-tuned LLM still produces numbers that can be wrong, and none was available here. Instead:

1. **Question understanding is trained.** Two TF-IDF (word + character n-gram) logistic-regression classifiers predict the *metric* (58 classes) and the *operation*. They were trained on 3,690 template questions in FinanceBench and TAT-QA style, with periods, names and numbers masked. The artifact is `finguard/xyz/model/xyz_intent.joblib` (with a metadata file). Deterministic parsers extract periods (FY, quarter, half-year, month, day, ranges), filters (segment/region/customer), grouping and top-k. A synonym lexicon backs the classifier up.
2. **Answers are computed, never generated.** Every number is an exact Decimal aggregation over the ledger (`metrics.py`). Each answer states the period, the formula, the accounts, the number of journal lines used and sample document references.
3. **Scope guard.** Other companies are refused. Periods outside the data return "no data", with no extrapolation. An unclear metric or missing period returns a clarification request.

Supported operations: value, change, % change (incl. YoY), CAGR, multi-year average, monthly average, total, ranking (customers/segments/regions), highest/lowest month/quarter/day, share of revenue, and "what drove the change" breakdowns by segment, region or cost line.

## Results (`evaluation/results/xyz/report.md`)

| Test set | n | Answer accuracy |
|---|---|---|
| FRESH — hand-written, frozen before error analysis (clean measure) | 35 | **97.1%** |
| FRESH refusals (other companies / future periods) | 5 | 100% |
| Template questions, unseen phrasings | 1,510 | 99.5% |
| FinanceBench questions rewritten for XYZ | 27 | 96.3% |
| TAT-QA frames rewritten for XYZ | 250 | 100% |
| Original FinanceBench questions about real companies (refusal) | 150 | 98.0% |
| Periods outside the data | 6 | 100% |

Template, FinanceBench and refusal sets were used for error analysis during development, so their numbers are
optimistic. FRESH was not.

## Use it

```bash
cd backend
DEMO_PASSWORD='...' .venv/bin/python -m finguard.xyz.cli setup     # org "xyz", users per role, ledger into PostgreSQL
.venv/bin/python -m finguard.xyz.cli train                          # rebuild corpus, retrain, re-evaluate
.venv/bin/python -m finguard.xyz.cli ask "What was revenue in FY2024?"
```

In the UI, sign in with organization `xyz` (e.g. `analyst@xyz.test`) and open **Company financials**:
- *Ask a question*, *Statements*, *Trends* and *Daily transactions* tabs.
- The **AI agent** routes financial questions to the governed `company_financials` tool (policy version `…+financials`).
- Contractors have no `financials:read` permission, and other organizations cannot see XYZ's ledger.

## Company records (non-metric questions)

`finguard/xyz/records.py` answers questions about XYZ Ltd that aren't a single figure. Like the metrics, these answers are read straight from the ledger, with Postgres and in-memory implementations that are tested against each other:

| Ask | Answered from |
|---|---|
| "What was the recent transaction made?", "Show the last 5 transactions", "Largest transaction in FY2025", "Transactions on 15 Aug 2019", "Recent customer receipts", "When was the last dividend paid?" | journal entries, ordered by date or by amount, filtered by type, period, counterparty, segment or region |
| "Details of SI-…" | a single entry, looked up by document reference |
| "Tell me about Orion Retail" | everything with that counterparty: invoiced/paid, revenue by FY, outstanding balance, latest transactions |
| "Which customers owe us the most?", "How much do we owe vendors?" | balances of receivables (1100) and payables (2000) by counterparty. These reconcile exactly to the balance sheet |
| "List all customers", "Who are our suppliers?" | counterparties grouped by sales or payments |
| "Tell me about XYZ Ltd", "What are the business segments?", "How is the company doing?" | profile and latest-FY key figures, with segment and region split |
| "What data do you have?", "Show the chart of accounts", "Types of transactions in FY2024" | ledger coverage, accounts with balances, transaction mix |

Facts the ledger doesn't record (CEO, headcount, address, share price) return **NO_DATA** and are never guessed. If a metric question names no period, the answer uses the latest data: balances as of 31 Mar 2026, flows for FY2026. That choice is stated as an assumption. Record questions only take over when the metric engine didn't confidently recognise a figure, and the metric evaluation is unchanged (template 99.27%, FinanceBench 96.30%, TAT-QA 100%, fresh 97.14%).

## Limitations

- English questions only.
- Qualitative FinanceBench-style questions (strategy, legal, segments the ledger doesn't model) are out of scope.
- "Drivers" answers are arithmetic decompositions, not causal explanations.
- The ledger is synthetic and simplified: no GST, accruals or deferred revenue, and interest paid is classified as financing cash flow.
