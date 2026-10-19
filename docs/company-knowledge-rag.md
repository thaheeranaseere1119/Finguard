# Company knowledge RAG (XYZ Ltd)

The finance agent answers two kinds of questions about the company:

| Question type | Examples | Answered by |
|---|---|---|
| **Figure** | "Revenue in FY2024", "last 10 months revenue", "top 5 customers", "largest transaction in FY2025" | Deterministic ledger engines (`metrics.py`, `records.py`): exact, computed, never generated |
| **Knowledge** | "Who is the CFO?", "What is the travel policy?", "What happened in May 2020?", "Who manages the Orion Retail account?", "Why did we borrow in 2020?" | **RAG** over the company log dataset |

Figure answers about changes or a customer profile also get a "Related company notes" block from RAG.

## The company log dataset

`python -m finguard.xyz.cli kb-build` generates the dataset into `evaluation/data/xyz/company_logs/` (`documents.jsonl` plus browsable `.md` files) and loads it into the knowledge base. All of it is synthetic and fictitious, and it's generated **from the same ledger**, so the narrative agrees with the books.

| Document type | Count | Content |
|---|---|---|
| company_profile | 1 | Business, leadership, board, headcount, bankers, auditors, calendar |
| policy | 6 | Expense and travel, procurement and vendor terms, credit and collection, treasury and dividend, contractor payments, tax |
| event_log | 18 | Dated company events (offices, launches, outage, ISO, the 2020 loan…) |
| board_minutes | 40 | Quarterly minutes: results versus the previous year, approvals (dividend, capex, loan), risks |
| customer_dossier / vendor_dossier | 48 / 16 | Relationship history, account manager, credit terms, lifetime value, notes |
| monthly_log | 120 | Monthly operations: revenue, margin, segments, top customers, collections, payroll, cash, notes |
| daily_transactions | 3,652 | Daily transaction log: counts and values by type, plus the largest entries |

## Pipeline (`finguard/xyz/kb.py`, `finguard/xyz/rag.py`)

1. **Chunking:** about 900 characters with 150 characters of overlap, split at headings and list items. Every chunk is prefixed with its document title, so a retrieved chunk is self-describing.
2. **Indexing:** chunks are stored in Postgres in `kb_documents` and `kb_chunks` (migration 0006). There are two indexes:
   - **Lexical:** a Postgres `tsvector` with a GIN index (English stemming).
   - **Semantic:** local embeddings (TF-IDF on 1–2-word terms → LSA / truncated SVD, 256 dimensions), trained on the company's own corpus. No external model or service is needed.
3. **Retrieval:** query expansion (CFO → chief financial officer, capex → capital expenditure…), then a lexical top 50, a semantic top 50, and a metadata top list (documents *about* a named customer or vendor). These are merged by **reciprocal-rank fusion**, with boosts for:
   - **period granularity:** a month question prefers the monthly log, a date question the daily log;
   - **named entity:** the subject of the question;
   - **document type implied by the question:** who → profile, policy words → policies, board → minutes.
4. **Relevance gate:** a retrieved passage must share at least half of the question's content words. Otherwise the answer is "not in the company records". It never answers from loosely related text.
5. **Generation:**
   - **With an LLM** (`LLM_PROVIDER=gemini|openai|anthropic` plus a key):
     - The model receives only the retrieved passages, as delimited, untrusted data, with account numbers and e-mails redacted.
     - It also receives exact figures from the ledger engine and must cite passage numbers.
     - Invented citations are rejected, and the answer falls back to quoting.
   - **Without an LLM** (default): extractive answers quote the best matching sentences, with rare words weighted higher and the document date kept. Nothing is paraphrased, so nothing can be invented.
6. **Safety:**
   - Retrieved records with instruction-like text are flagged and treated as data.
   - External companies are refused before retrieval.
   - Contractors have no access (`financials:read`).

## Evaluation (`python -m finguard.xyz.cli kb-eval` → `evaluation/results/xyz/rag_report.md`)

Measured on the PostgreSQL path with the offline extractive generator (no LLM key):

| Split | n | hit@5 | Answer accuracy | Refusal accuracy |
|---|---|---|---|---|
| Tuning (retrieval was tuned on these) | 53 | 98.1% | 98.1% | 100% |
| Held-out (written after tuning) | 25 | 76.0% | 60.0% | 100% |

**Honesty notes.**
- The tuning split is optimistic by construction.
- The held-out split's first, blind run (in-memory index) scored **40% answer accuracy / 80% hit@5**. After general, not question-specific, fixes it scores 60–68%, but that run is no longer blind.
- The remaining misses are mostly questions that need a short inference or summary ("Is Krypton a component supplier?"). That's what the LLM generation step is for; it hasn't been measured, because no LLM key is configured.
- The original figure-question evaluation is unchanged with the knowledge base loaded: template 99.27%, FinanceBench 96.30%, TAT-QA 100%, fresh 97.14%.

## Enabling the LLM

Set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=…` in `.env` (or `openai` / `anthropic` with their keys), then restart the API. Before using real company data, review the data terms of the provider you choose; with synthetic XYZ data there is no exposure.
