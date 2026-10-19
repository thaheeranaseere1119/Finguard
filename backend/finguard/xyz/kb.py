"""Company knowledge base: chunking, local semantic embeddings and hybrid retrieval (RAG retriever).

Pipeline
  documents → contextual chunks (the document title is prefixed to every chunk; ~900 chars with overlap)
            → two indexes
                 * lexical: PostgreSQL full-text search (tsvector, GIN index, ts_rank_cd)  — exact names, refs, dates
                 * semantic: TF-IDF (word 1–2-grams) → LSA (truncated SVD, 256 dims), unit-normalised vectors stored
                   per chunk — paraphrases ("who runs the company" ≈ "Managing Director & CEO")
            → hybrid ranking: reciprocal-rank fusion of both lists + metadata boosts (period overlap, named entity,
              document type implied by the question)
No external service or model download is needed. Two interchangeable stores implement the same retrieval:
SQLKB (PostgreSQL — API/agent) and MemoryKB (in-process — tests and offline evaluation).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

CHUNK_CHARS = 900
OVERLAP_CHARS = 150
EMBED_DIM = 256
RRF_K = 60

TYPE_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*who\b|\b(ceo|cfo|coo|cto|founder|founded|leadership|managing director|chair|board of directors|head office|bank|"
                r"headquarter|registered|how many employees|headcount|auditor|banker|who (?:runs|owns|leads)|about the company)\b", re.I),
     "company_profile"),
    (re.compile(r"\b(polic(?:y|ies)|rules?|allowed|limit|cap|per diem|terms|credit terms|payment terms|dso|dividend policy|"
                r"travel|hotel|procurement|approval needed|minimum|maximum|target|must|needs? approval|who approves)\b", re.I), "policy"),
    (re.compile(r"\b(board|minutes|meeting|resolution|approved by the board|ratified)\b", re.I), "board_minutes"),
    (re.compile(r"\b(account manager|customer since|credit terms for|relationship|dossier|best year)\b", re.I), "customer_dossier"),
    (re.compile(r"\b(supplier|vendor terms|role of|'s role|kyc|what does \w+ (?:supply|provide))\b", re.I), "vendor_dossier"),
    (re.compile(r"\b(event|happened|launched|opened|outage|certif|iso|office|lockdown|milestone|won)\b", re.I), "event_log"),
]


# Query expansion: company/finance vocabulary the records use in a different form than people ask.
EXPANSIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bceo\b|\bruns? the company\b|\bleads? the company\b|\bhead of the company\b", re.I),
     "managing director ceo chief executive"),
    (re.compile(r"\bcfo\b", re.I), "chief financial officer finance"),
    (re.compile(r"\bcoo\b", re.I), "chief operating officer operations"),
    (re.compile(r"\bcto\b", re.I), "chief technology officer technology"),
    (re.compile(r"\bhr\b|\bhuman resources\b", re.I), "head of human resources hiring"),
    (re.compile(r"\b(hq|headquarter\w*|based|located)\b", re.I), "head office registered bengaluru"),
    (re.compile(r"\bhow many (employees|staff|people)|\bheadcount|\bworkforce|\bteam size", re.I), "headcount employees"),
    (re.compile(r"\bauditors?\b", re.I), "statutory auditors"),
    (re.compile(r"\b(which|what|who)\b.*\bbank(er)?s?\b|\bbanker", re.I), "banker lender coral bank"),
    (re.compile(r"\bloan|borrow\w*|debt\b", re.I), "loan term loan liquidity facility coral bank"),
    (re.compile(r"\biso\b|certif\w*", re.I), "iso iec 27001 recertified certification"),
    (re.compile(r"\boutage|downtime|incident\b", re.I), "outage power failure data centre"),
    (re.compile(r"\bwhat happened|events?|news\b", re.I), "note event"),
    (re.compile(r"\bcovid|pandemic|lockdown\b", re.I), "covid-19 lockdown"),
    (re.compile(r"\bhotel|accommodation|stay\b", re.I), "hotel cap per night"),
    (re.compile(r"\bminimum|at least|keeps?|maintain\w*\b", re.I), "at least maintains minimum"),
    (re.compile(r"\bcapex\b|capital expenditure", re.I), "capital expenditure capex"),
    (re.compile(r"\bapprov\w*|sign[- ]?off\b", re.I), "approval approved sign-off"),
    (re.compile(r"\baffected|impact\w*|how was business\b", re.I), "note fell sharply recovering lockdown"),
    (re.compile(r"\bvendor|supplier\b", re.I), "vendor supplier payment terms"),
]


def expand_query(q: str) -> str:
    extra = [e for rx, e in EXPANSIONS if rx.search(q)]
    return q + (" " + " ".join(extra) if extra else "")


@dataclass
class Chunk:
    id: int
    doc_key: str
    doc_type: str
    title: str
    text: str
    period_start: date | None = None
    period_end: date | None = None
    entities: list[str] = field(default_factory=list)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    lexical_rank: int | None
    semantic_rank: int | None
    why: list[str]


# ------------------------------------------------------------------------------------------------ chunking
def chunk_document(doc: dict) -> list[str]:
    """Split on blank lines / headings / list items, pack into ~CHUNK_CHARS windows with overlap, and prefix every
    chunk with the document title so a chunk is self-describing when retrieved on its own."""
    text = doc["text"]
    header = f"[{doc['title']}]"
    if len(text) <= CHUNK_CHARS + OVERLAP_CHARS:
        return [f"{header}\n{text}"]
    units = [u for u in re.split(r"\n(?=#|- |\n)", text) if u.strip()]
    chunks, cur = [], ""
    for u in units:
        if len(cur) + len(u) > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = cur[-OVERLAP_CHARS:] + "\n" + u
        else:
            cur = (cur + "\n" + u) if cur else u
    if cur.strip():
        chunks.append(cur)
    return [f"{header}\n{c.strip()}" for c in chunks]


# ------------------------------------------------------------------------------------------------ embeddings
class Embedder:
    """Local semantic embedding model: TF-IDF → LSA. Fitted on the organisation's own corpus."""

    def __init__(self, vectorizer, svd) -> None:
        self.vec, self.svd = vectorizer, svd

    @classmethod
    def fit(cls, texts: list[str], dim: int = EMBED_DIM) -> "Embedder":
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        small = len(texts) < 20                       # small knowledge bases: keep every term
        vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1 if small else 2, max_df=1.0 if small else 0.6,
                              sublinear_tf=True, stop_words="english",
                              token_pattern=r"(?u)\b[\w₹][\w.\-&]*\b", max_features=200_000)
        X = vec.fit_transform(texts)
        svd = TruncatedSVD(n_components=max(1, min(dim, X.shape[1] - 1, X.shape[0] - 1 if X.shape[0] > 1 else 1)),
                           random_state=7).fit(X)
        return cls(vec, svd)

    def encode(self, texts: list[str]) -> np.ndarray:
        Z = self.svd.transform(self.vec.transform(texts)).astype(np.float32)
        n = np.linalg.norm(Z, axis=1, keepdims=True)
        n[n == 0] = 1
        return Z / n

    def save(self, path: Path) -> None:
        import joblib

        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"vec": self.vec, "svd": self.svd}, path)

    @classmethod
    def load(cls, path: Path) -> "Embedder":
        import joblib

        d = joblib.load(path)
        return cls(d["vec"], d["svd"])


def embedder_path(org_id) -> Path:
    from finguard.core.config import get_settings

    return Path(get_settings().storage_dir).resolve() / "kb" / f"{org_id}.joblib"


# ------------------------------------------------------------------------------------------------ query analysis
def _query_context(q: str) -> tuple[list, list[str], set[str]]:
    from finguard.xyz.periods import find_periods
    from finguard.xyz.records import PARTIES

    periods = find_periods(q)
    # a bare year ("in 2020") is read as a fiscal year by the metric engine; for retrieval also cover the calendar year
    from finguard.xyz.periods import Period

    for p in list(periods):
        if p.kind == "FY" and p.assumption and re.search(rf"\b{p.fy}\b", q):
            periods.append(Period(f"CY{p.fy}", date(p.fy, 1, 1), date(p.fy, 12, 31), "CY", None))
    low = q.lower()
    ents = [p for p in PARTIES if p.lower() in low]
    types = {t for rx, t in TYPE_HINTS if rx.search(q)}
    return periods, ents, types


_TXN_WORDS = re.compile(r"\b(transactions?|entries|invoices?|receipts?|payments?|paid|largest|biggest|sales? on)\b", re.I)


def _boosts(c: Chunk, periods, ents, types, txn_q: bool = False) -> tuple[float, list[str]]:
    b, why = 0.0, []
    if periods and c.period_start:
        for p in periods:
            if c.period_start <= p.end and (c.period_end or c.period_start) >= p.start:
                span = ((c.period_end or c.period_start) - c.period_start).days
                qspan = (p.end - p.start).days
                # granularity match: a month question prefers the monthly log, a date question the daily log
                if (span == 0 and qspan == 0) or (0 < span <= 31 and 0 < qspan <= 31):
                    b += 0.035
                elif span == 0 and qspan > 0:
                    b += 0.008
                else:
                    b += 0.018
                why.append(f"period {p.label}")
                break
    if c.doc_type == "daily_transactions" and not txn_q and not any((p.end - p.start).days == 0 for p in periods):
        b -= 0.012                                           # 3,652 daily logs must not crowd out profile/policy/events
    for e in ents:
        if c.entities == [e] or (e in c.entities and c.doc_type == "event_log"):   # the document is ABOUT this entity
            b += 0.04
            why.append(f"about {e}")
        elif e in c.entities or e.lower() in c.text.lower():
            b += 0.015
            why.append(f"mentions {e}")
    if c.doc_type in types:
        b += 0.025
        why.append(f"{c.doc_type.replace('_', ' ')} matches the question")
    return b, why


def _phrases(q: str) -> list[str]:
    """Adjacent content-word pairs of the question ("business class", "vendor payment")."""
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    toks = [t.lower() for t in _TOKEN.findall(q)]
    return [f"{a} {b}" for a, b in zip(toks, toks[1:]) if a not in ENGLISH_STOP_WORDS and b not in ENGLISH_STOP_WORDS
            and a not in ("xyz", "ltd") and b not in ("xyz", "ltd")]


def _period_ids(chunks: dict[int, Chunk], periods, types: set[str]) -> list[int]:
    """Metadata-filter retrieval by period: documents whose own period overlaps the question's (board minutes for
    "the December 2023 quarter" say "Q3 FY2024", so neither words nor embeddings can connect them)."""
    if not periods:
        return []
    want = (types & {"board_minutes", "monthly_log", "event_log"}) or {"monthly_log", "board_minutes", "event_log"}
    if any((p.end - p.start).days == 0 for p in periods):
        want = want | {"daily_transactions"}
    out = []
    for cid, c in chunks.items():
        if c.doc_type in want and c.period_start and any(
                c.period_start <= p.end and (c.period_end or c.period_start) >= p.start for p in periods):
            out.append(cid)
    return out[:40]


def _subject_ids(chunks: dict[int, Chunk], ents: list[str]) -> list[int]:
    """Metadata-filter retrieval: chunks of documents whose subject is a named customer/vendor (dossiers, events)."""
    if not ents:
        return []
    return [cid for cid, c in chunks.items() if any(e in c.entities for e in ents) and c.doc_type != "daily_transactions"
            and len(c.entities) <= 3]


def _fuse(lex: list[int], sem: list[int], chunks: dict[int, Chunk], q: str, k: int) -> list[Hit]:
    periods, ents, types = _query_context(q)
    txn_q = bool(_TXN_WORDS.search(q))
    lr = {cid: i for i, cid in enumerate(lex)}
    sr = {cid: i for i, cid in enumerate(sem)}
    subj = {cid: i for i, cid in enumerate(_subject_ids(chunks, ents))}
    per = {cid: i for i, cid in enumerate(_period_ids(chunks, periods, types))}
    phrases = _phrases(q)
    out = []
    for cid in set(lr) | set(sr) | set(subj) | set(per):
        c = chunks.get(cid)
        if c is None:
            continue
        s = ((1 / (RRF_K + lr[cid]) if cid in lr else 0) + (1 / (RRF_K + sr[cid]) if cid in sr else 0)
             + (1 / (RRF_K + subj[cid]) if cid in subj else 0) + (1 / (RRF_K + per[cid]) if cid in per else 0))
        b, why = _boosts(c, periods, ents, types, txn_q)
        low = c.text.lower()
        ph = [p for p in phrases if p in low]
        if ph:
            b += min(0.04, 0.02 * len(ph))
            why.append(f"phrase “{ph[0]}”")
        out.append(Hit(c, s + b, lr.get(cid), sr.get(cid), why))
    out.sort(key=lambda h: -h.score)
    return out[:k]


_TOKEN = re.compile(r"[A-Za-z0-9]{2,}")


def _ts_query(q: str) -> str:
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    toks = [t.lower() for t in _TOKEN.findall(q) if t.lower() not in ENGLISH_STOP_WORDS and t.lower() not in
            {"xyz", "ltd", "company", "tell", "did", "does", "know", "please"}]
    return " | ".join(dict.fromkeys(toks))


# ------------------------------------------------------------------------------------------------ stores
def _stem_analyzer():
    from finguard.xyz.rag import _terms

    return lambda text: sorted(_terms(text))


class MemoryKB:
    """In-process index over a corpus (tests / offline evaluation). Lexical side uses stemmed TF-IDF cosine
    (the SQL store gets stemming from PostgreSQL's English text-search configuration)."""

    def __init__(self, docs: list[dict]) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.chunks: dict[int, Chunk] = {}
        texts = []
        for d in docs:
            ps = date.fromisoformat(d["period_start"]) if d.get("period_start") else None
            pe = date.fromisoformat(d["period_end"]) if d.get("period_end") else None
            for t in chunk_document(d):
                cid = len(self.chunks) + 1
                self.chunks[cid] = Chunk(cid, d["doc_id"], d["doc_type"], d["title"], t, ps, pe, d.get("entities", []))
                texts.append(t)
        self.ids = list(self.chunks)
        self.embedder = Embedder.fit(texts)
        self.E = self.embedder.encode(texts)
        self.lex = TfidfVectorizer(analyzer=_stem_analyzer(), sublinear_tf=True).fit(texts)
        self.L = self.lex.transform(texts)

    def search(self, q: str, k: int = 6) -> list[Hit]:
        raw, q = q, expand_query(q)
        lex_scores = (self.L @ self.lex.transform([q]).T).toarray().ravel()
        lex = [self.ids[i] for i in np.argsort(-lex_scores)[:50] if lex_scores[i] > 0]
        sem_scores = self.E @ self.embedder.encode([q])[0]
        sem = [self.ids[i] for i in np.argsort(-sem_scores)[:50]]
        return _fuse(lex, sem, self.chunks, raw, k)


class SQLKB:
    """PostgreSQL store: lexical retrieval by full-text search; semantic retrieval over stored embeddings."""

    _cache: dict = {}
    _lock = threading.Lock()

    def __init__(self, db, org_id) -> None:
        self.db, self.org = db, org_id

    def available(self) -> bool:
        from sqlalchemy import text

        return self.db.execute(text("SELECT 1 FROM kb_chunks WHERE org_id = :o LIMIT 1"), {"o": self.org}).first() is not None

    def _matrix(self):
        """Chunk metadata + embedding matrix, cached per org until the chunk count changes."""
        from sqlalchemy import text

        n = self.db.execute(text("SELECT count(*), max(id) FROM kb_chunks WHERE org_id = :o"), {"o": self.org}).one()
        key = (str(self.org), int(n[0]), int(n[1] or 0))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        rows = self.db.execute(text(
            "SELECT c.id, d.doc_key, d.doc_type, d.title, c.text, d.period_start, d.period_end, d.entities, c.embedding "
            "FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id WHERE c.org_id = :o ORDER BY c.id"),
            {"o": self.org}).all()
        chunks = {r[0]: Chunk(r[0], r[1], r[2], r[3], r[4], r[5], r[6], list(r[7] or [])) for r in rows}
        ids = [r[0] for r in rows]
        E = np.array([r[8] for r in rows], dtype=np.float32) if rows and rows[0][8] is not None else None
        emb = Embedder.load(embedder_path(self.org)) if embedder_path(self.org).exists() else None
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != key[0]}
            self._cache[key] = (chunks, ids, E, emb)
        return self._cache[key]

    def search(self, q: str, k: int = 6) -> list[Hit]:
        from sqlalchemy import text

        chunks, ids, E, emb = self._matrix()
        raw, q = q, expand_query(q)
        tsq = _ts_query(q)
        lex = []
        if tsq:
            lex = [r[0] for r in self.db.execute(text(
                "SELECT id FROM kb_chunks WHERE org_id = :o AND tsv @@ to_tsquery('english', :q) "
                "ORDER BY ts_rank_cd(tsv, to_tsquery('english', :q)) DESC LIMIT 50"), {"o": self.org, "q": tsq}).all()]
        sem = []
        if E is not None and emb is not None:
            scores = E @ emb.encode([q])[0]
            sem = [ids[i] for i in np.argsort(-scores)[:50]]
        return _fuse(lex, sem, chunks, raw, k)


def ingest(db, org_id, docs: list[dict]) -> dict:
    """Replace the organisation's knowledge base: store documents, chunk them, fit the embedder, store vectors."""
    from sqlalchemy import text

    db.execute(text("DELETE FROM kb_chunks WHERE org_id = :o"), {"o": org_id})
    db.execute(text("DELETE FROM kb_documents WHERE org_id = :o"), {"o": org_id})
    import json

    rows = []
    for d in docs:
        doc_id = db.execute(text(
            "INSERT INTO kb_documents (org_id, doc_key, doc_type, title, period_start, period_end, entities) "
            "VALUES (:o, :k, :t, :ti, :ps, :pe, CAST(:en AS jsonb)) RETURNING id"),
            {"o": org_id, "k": d["doc_id"], "t": d["doc_type"], "ti": d["title"][:300], "ps": d.get("period_start"),
             "pe": d.get("period_end"), "en": json.dumps(d.get("entities", []))}).scalar_one()
        for i, t in enumerate(chunk_document(d)):
            rows.append((doc_id, i, t))
    emb = Embedder.fit([t for _, _, t in rows])
    vecs = emb.encode([t for _, _, t in rows])
    conn = db.connection().connection
    with conn.cursor() as cur:
        with cur.copy("COPY kb_chunks (org_id, document_id, chunk_no, text, embedding) FROM STDIN") as cp:
            for (doc_id, i, t), v in zip(rows, vecs):
                cp.write_row((org_id, doc_id, i, t, "{" + ",".join(f"{x:.5f}" for x in v) + "}"))
    emb.save(embedder_path(org_id))
    return {"documents": len(docs), "chunks": len(rows), "embedding_dim": int(vecs.shape[1])}
