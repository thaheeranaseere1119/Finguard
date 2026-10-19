# XYZ Ltd company-knowledge RAG — evaluation

Retriever: PostgreSQL full-text search + local TF-IDF/LSA embeddings, reciprocal-rank fusion with metadata boosts. Generator: configured LLM.

| Split | n | hit@1 | hit@5 | answer accuracy | refusal accuracy | p50 latency |
|---|---|---|---|---|---|---|
| tuning | 53 | 90.6% | 98.1% | 98.1% | 100.0% | 45100 ms |
| held_out | 25 | 76.0% | 96.0% | 76.0% | 100.0% | 45128 ms |

**Honesty notes.** Retrieval and routing were tuned on the *tuning* split, so its score is optimistic. The *held-out* split was written after tuning; its first (blind) run scored 40.0% answer accuracy and 80.0% hit@5 on the in-memory index. General (not question-specific) fixes were then made, so later held-out runs are no longer blind. All facts are synthetic.

## Misses

- [tuning] Who must approve capital expenditure above 5 crore? — expected “board approval”; got: Operations log March 2022: Note: Capital expenditure ₹12.00 crore on 8 March 2022 (CAP-20220308-0093885, supplier Juno Power Systems) — new data centre build-ou
- [held_out] Who looks after treasury and tax? — expected “Nandini Shah”; got: That isn't in XYZ Ltd's company records or ledger, so I can't answer it without guessing.
- [held_out] Can employees fly business class? — expected “CFO approval”; got: XYZ Ltd is an Indian technology company with three segments (Enterprise Software, Cloud Services, Hardware Solutions) selling to 48 business customers across 5 
- [held_out] How much interest do fixed deposits earn? — expected “4%”; got: The most recent transaction was Interest on deposits — March 2026 on 31 Mar 2026 (INT-20260331-0190014, ₹85.96 lakh, Coral Bank). 10 most recent interest paymen
- [held_out] What did the board note in the December 2023 quarter? — expected “27001”; got: Operations log December 2023: Note: Capital expenditure ₹2.17 crore on 15 December 2023 (CAP-20231215-0132457, supplier Helix Hardware).
- [held_out] What does the operations log say about October 2019? — expected “Festival-season”; got: Operations log October 2019: Note: Advance tax of ₹2.27 crore paid on 15 October 2019. Operations log October 2019: Revenue: ₹19.48 crore (+2.9% vs October 2018
- [held_out] Is Krypton Data Centres a component supplier? — expected “cloud capacity provider”; got: Payment terms: 10th of the month after next. Approved component suppliers: Alpha Components, Beacon Supplies, Crest Semiconductors, Dune Packaging, Helix Hardwa
