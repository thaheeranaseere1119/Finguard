# XYZ Ltd financial Q&A — training & evaluation

Imaginary company, synthetic 10-year daily ledger (FY2017–FY2026). Answers are computed exactly from the ledger; the trained model only interprets the question (metric + operation). Gold answers come from the generator's in-memory ledger via the gold plan (independent of the parser and SQL).

Training questions: 3690 (template phrasings A). Test sets use unseen phrasings.

| Test set | n | Answer accuracy | Status accuracy | Metric (pipeline) | Operation (pipeline) | Metric (classifier only) | Metric (lexicon only) | p95 latency ms |
|---|---|---|---|---|---|---|---|---|
| TEMPLATE | 1510 | 99.3% | 100.0% | 99.9% | 99.3% | 99.8% | 97.0% | 44 |
| FINANCEBENCH | 27 | 96.3% | 100.0% | 96.3% | 92.6% | 85.2% | 92.6% | 28 |
| TATQA | 250 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 48 |
| OOS | 150 | 98.0% | 98.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0 |
| OUT_OF_RANGE | 6 | 100.0% | 100.0% | 0.0% | 0.0% | 0.0% | 0.0% | 8 |
| FRESH | 35 | 97.1% | 100.0% | 100.0% | 94.3% | 100.0% | 97.1% | 27 |
| FRESH_REFUSAL | 5 | 100.0% | 100.0% | 0.0% | 0.0% | 0.0% | 0.0% | 7 |

TEMPLATE = held-out phrasings of generated questions · FINANCEBENCH = real FinanceBench questions rewritten for XYZ Ltd · TATQA = real TAT-QA arithmetic frames rewritten · OOS = original FinanceBench questions about real companies (must be refused) · OUT_OF_RANGE = periods outside the data (must answer 'no data').

## How these numbers were produced (read before citing)

* Development used error analysis on the TEMPLATE, FINANCEBENCH and OOS test sets (parser fixes and corrections to the FinanceBench rewrite labels), so those scores are optimistic.
* FRESH is the clean measure: hand-written questions in finguard/xyz/fresh_test.py, frozen before any error analysis. Answer accuracy 97.1%; refusals 100%.
* The model never trains on FRESH, FINANCEBENCH, TATQA or OOS questions. Gold answers are computed on the generator's in-memory ledger from the labelled plan, independent of the parser and SQL.
* Some OOS FinanceBench questions name no company; answering them for XYZ Ltd still counts as an error here.

## Sample errors

* [TEMPLATE] What was the cumulative earnings across FY 2018-19, 2019-20 and 2020-21? — expected ANSWERED net_income/SUM gold=720095496.00; got ANSWERED gross_profit/SUM 3370143514.00
* [TEMPLATE] How much did Sapphire Jewels contribute in gross profit in FY2022? — expected ANSWERED gross_profit/VALUE gold=14654436.00; got ANSWERED gross_profit/CHANGE 1641802.00
* [TEMPLATE] How much did Coral Bank contribute in gross profit in financial year 2023-24? — expected ANSWERED gross_profit/VALUE gold=66056715.00; got ANSWERED gross_profit/CHANGE 9667837.00
* [TEMPLATE] How much did Ember Chemicals contribute in turnover in fiscal year 2022? — expected ANSWERED revenue/VALUE gold=24971544.00; got ANSWERED revenue/CHANGE -1746827.00
* [TEMPLATE] How much did Comet Couriers contribute in gross profit in fiscal year 2021? — expected ANSWERED gross_profit/VALUE gold=12710574.00; got ANSWERED gross_profit/CHANGE -71847.00
* [TEMPLATE] How much did Aurora Hotels contribute in cogs in FY 2023-24? — expected ANSWERED cogs/VALUE gold=25364385.00; got ANSWERED cogs/CHANGE 1863748.00
* [TEMPLATE] How much did Pinnacle Insurance contribute in gross profit in FY 2025-26? — expected ANSWERED gross_profit/VALUE gold=52440881.00; got ANSWERED gross_profit/CHANGE 7390733.00
* [TEMPLATE] How much did Summit Realty contribute in gross profit in FY24? — expected ANSWERED gross_profit/VALUE gold=28262273.00; got ANSWERED gross_profit/CHANGE 6800613.00
* [TEMPLATE] How much did Opal Cosmetics contribute in gross profit in fiscal year 2019? — expected ANSWERED gross_profit/VALUE gold=13477237.00; got ANSWERED gross_profit/CHANGE -866569.00
* [TEMPLATE] How much did Maple Education contribute in gross profit in fiscal year 2022? — expected ANSWERED gross_profit/VALUE gold=17422107.00; got ANSWERED gross_profit/CHANGE -2284743.00
* [TEMPLATE] How much did Vertex Pharma contribute in gross profit in FY18? — expected ANSWERED gross_profit/VALUE gold=39721661.00; got ANSWERED gross_profit/CHANGE 11621577.00
* [FINANCEBENCH] We need to calculate a reasonable approximation (or exact number if possible) of a financial metric. Basing your judgment by information plainly provided in the statement of income, what is XYZ Ltd three year average of cost of goods sold as a % of revenue from FY2020 to FY2022? Answer in units of percents and round to one decimal place. — expected ANSWERED revenue/AVERAGE gold=2179055132.00; got ANSWERED cogs/AVERAGE 913098105.00
* [OOS] Was there any drop in Cash & Cash equivalents between FY 2023 and Q2 of FY2024? — expected OUT_OF_SCOPE None/VALUE gold=None; got ANSWERED cash/VALUE 1364530281.00
* [OOS] Were there any board member nominees who had substantially more votes against joining than the other nominees? — expected OUT_OF_SCOPE None/VALUE gold=None; got NO_DATA None/None None
* [OOS] What drove the reduction in SG&A expense as a percent of net sales in FY2023? — expected OUT_OF_SCOPE None/VALUE gold=None; got ANSWERED revenue/DRIVERS 350886583.00
* [FRESH] What was the gross margin percentage in FY2025? — expected ANSWERED gross_margin/VALUE gold=58.23; got ANSWERED gross_margin/PCT_CHANGE 1.15
