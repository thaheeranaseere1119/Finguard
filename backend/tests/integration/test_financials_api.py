"""Company financials API + agent tool over a real PostgreSQL ledger (XYZ Ltd data loaded into a test org)."""

import pytest
from fastapi.testclient import TestClient

from finguard.main import app
from finguard.xyz.generator import generate, load_into_db
from tests.conftest import PASSWORD


@pytest.fixture(scope="module")
def ledger():
    return generate()


def _login(client, o, key):
    r = client.post("/api/v1/auth/login", json={"organization": o.slug, "email": o.users[key].email, "password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_financial_qa_scoping_and_agent(db, make_org, ledger):
    from finguard.core.ratelimit import reset

    reset()
    xyz, other = make_org("xyzco"), make_org("other")
    load_into_db(db, xyz.org.id, ledger)
    db.commit()
    client = TestClient(app)
    h = _login(client, xyz, "analyst")
    r = client.post("/api/v1/financials/ask", headers=h, json={"question": "What was XYZ Ltd's revenue in FY2024?"})
    assert r.status_code == 200 and r.json()["status"] == "ANSWERED"
    assert r.json()["evidence"][0]["journal_lines"] > 0 and r.json()["plan"]["metric"] == "revenue"
    assert client.post("/api/v1/financials/ask", headers=h, json={"question": "What was Apple's revenue in FY2024?"}).json()["status"] == "OUT_OF_SCOPE"
    st = client.get("/api/v1/financials/statements?fy=2024", headers=h).json()
    assert {x["key"] for x in st["income_statement"]} >= {"revenue", "net_income"}
    tx = client.get("/api/v1/financials/transactions?date_from=2019-08-15&date_to=2019-08-15", headers=h).json()
    assert tx["total"] > 0 and all(x["date"] == "2019-08-15" for x in tx["items"])
    # contractors have no access to company financials; other tenants cannot see this ledger
    assert client.post("/api/v1/financials/ask", headers=_login(client, xyz, "contractor"), json={"question": "revenue FY2024"}).status_code == 403
    assert client.get("/api/v1/financials/overview", headers=_login(client, other, "analyst")).status_code == 404
    # the agent routes financial questions to the governed company_financials tool
    run = client.post("/api/v1/agent/chat", headers=h, json={"message": "What was the net profit margin in FY2023?"}).json()
    assert [t["tool"] for t in run["tool_calls"]] == ["company_financials"] and "%" in run["answer"]


def test_agent_returns_structured_output(db, make_org, ledger):
    from finguard.core.ratelimit import reset

    reset()
    xyz = make_org("xyzst")
    load_into_db(db, xyz.org.id, ledger)
    db.commit()
    client = TestClient(app)
    h = _login(client, xyz, "analyst")
    run = client.post("/api/v1/agent/chat", headers=h, json={"message": "Give me the last 6 months revenue"}).json()
    st = run["structured"]
    assert st["status"] == "ANSWERED" and st["summary"] and st["title"].startswith("Revenue")
    kinds = [b["type"] for b in st["blocks"]]
    assert kinds[0] == "highlights" and "table" in kinds
    table = next(b for b in st["blocks"] if b["type"] == "table")
    assert len(table["rows"]) == 6 and table["columns"][0] == "Period"
    assert st["assumptions"] and st["sources"]
    top = client.post("/api/v1/agent/chat", headers=h, json={"message": "Top 5 customers by revenue in FY2025"}).json()["structured"]
    assert next(b for b in top["blocks"] if b["type"] == "table")["columns"] == ["Rank", "Customer", "Revenue from operations", "Share of total"]
    oos = client.post("/api/v1/agent/chat", headers=h, json={"message": "What was Apple's revenue in FY2024?"}).json()["structured"]
    assert oos["status"] == "OUT_OF_SCOPE"


def test_agent_answers_company_record_questions(db, make_org, ledger):
    """Non-metric company questions (transactions, customers, profile) are answered from the ledger, not 'please clarify'."""
    from finguard.core.ratelimit import reset

    reset()
    xyz = make_org("xyzrec")
    load_into_db(db, xyz.org.id, ledger)
    db.commit()
    client = TestClient(app)
    h = _login(client, xyz, "analyst")
    last = max(ledger.entries, key=lambda e: (e.d, e.id))
    run = client.post("/api/v1/agent/chat", headers=h, json={"message": "what was the recent transaction made?"}).json()
    assert [t["tool"] for t in run["tool_calls"]] == ["company_financials"]
    st = run["structured"]
    assert st["status"] == "ANSWERED" and last.doc_ref in st["summary"]
    table = next(b for b in st["blocks"] if b["type"] == "table")
    assert table["columns"][:3] == ["Date", "Reference", "Type"] and table["rows"][0][1] == last.doc_ref
    for q in ("When was the last dividend paid?", "Which customers owe us the most?", "Tell me about XYZ Ltd",
              "Show the last 5 invoices", "Tell me about Orion Retail"):
        r = client.post("/api/v1/agent/chat", headers=h, json={"message": q}).json()
        assert [t["tool"] for t in r["tool_calls"]] == ["company_financials"], q
        assert r["structured"]["status"] == "ANSWERED", (q, r["structured"]["summary"])
    # the contractor still has no access to company records
    c = client.post("/api/v1/agent/chat", headers=_login(client, xyz, "contractor"),
                    json={"message": "what was the recent transaction made?"}).json()
    assert "company_financials" not in [t["tool"] for t in c["tool_calls"] if t.get("status") == "OK"]
    assert last.doc_ref not in (c.get("answer") or "")


def test_rag_over_company_knowledge_base_via_api_and_agent(db, make_org, ledger):
    """Knowledge questions are answered from the PostgreSQL knowledge base (full-text + embeddings) with citations;
    figures still come from the ledger; contractors still have no access."""
    from finguard.core.ratelimit import reset
    from finguard.xyz import kb
    from finguard.xyz.kb_corpus import build_corpus

    reset()
    xyz = make_org("xyzrag")
    load_into_db(db, xyz.org.id, ledger)
    stats = kb.ingest(db, xyz.org.id, build_corpus(ledger))
    db.commit()
    assert stats["chunks"] > 7000 and stats["embedding_dim"] > 100
    client = TestClient(app)
    h = _login(client, xyz, "analyst")
    r = client.post("/api/v1/financials/ask", headers=h, json={"question": "Who is the CFO of XYZ?"}).json()
    assert r["status"] == "ANSWERED" and "Nandini Shah" in r["answer"]
    assert r["structured"]["plan"]["operation"].startswith("RAG:") and r["evidence"][0]["id"].startswith("kb:")
    run = client.post("/api/v1/agent/chat", headers=h, json={"message": "What are the standard vendor payment terms?"}).json()
    assert [t["tool"] for t in run["tool_calls"]] == ["company_financials"]
    assert "30 to 45 days" in run["structured"]["summary"]
    fig = client.post("/api/v1/financials/ask", headers=h, json={"question": "What was XYZ Ltd's revenue in FY2024?"}).json()
    assert fig["plan"]["metric"] == "revenue"                      # exact figure from the ledger, not from retrieval
    assert client.post("/api/v1/financials/ask", headers=_login(client, xyz, "contractor"),
                       json={"question": "Who is the CFO of XYZ?"}).status_code == 403
