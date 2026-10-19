"""API-level authentication and authorization (direct API calls, not UI) — required test 17 and security controls."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from finguard.main import app
from finguard.models import ApprovalRequest, AuditLog
from tests.conftest import PASSWORD, contract_pdf, onboard


@pytest.fixture
def client():
    from finguard.core.ratelimit import reset

    reset()
    return TestClient(app)


def login(client, o, key):
    r = client.post("/api/v1/auth/login", json={"organization": o.slug, "email": o.users[key].email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_unauthenticated_requests_rejected(client):
    for path in ("/api/v1/contracts", "/api/v1/payments", "/api/v1/approvals", "/api/v1/audit", "/api/v1/users"):
        assert client.get(path).status_code == 401
    assert client.get("/api/v1/contracts", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_17_cross_organization_access_denied(client, db, make_org):
    a, b = make_org("alpha"), make_org("beta")
    c = onboard(db, a)
    hb = login(client, b, "manager")
    assert client.get(f"/api/v1/contracts/{c.id}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/contracts/{c.id}/extraction", headers=hb).status_code == 404
    assert client.get(f"/api/v1/documents/{c.primary_document_id}/download", headers=hb).status_code == 404
    lst = client.get("/api/v1/contracts", headers=hb).json()
    assert all(item["id"] != str(c.id) for item in lst["items"])
    pays = client.get(f"/api/v1/payments?contract_id={c.id}", headers=hb).json()
    assert pays["total"] == 0
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == c.id)).scalars().first()
    r = client.post(f"/api/v1/approvals/{ar.id}/decision", headers=hb, json={"decision": "APPROVE", "reason": "cross tenant"})
    assert r.status_code == 404


def test_contractor_sees_only_own_contracts(client, db, make_org):
    o = make_org()
    c = onboard(db, o)
    h2 = login(client, o, "contractor2")
    assert client.get(f"/api/v1/contracts/{c.id}", headers=h2).status_code == 404
    assert client.get("/api/v1/contracts", headers=h2).json()["total"] == 0
    h1 = login(client, o, "contractor")
    assert client.get(f"/api/v1/contracts/{c.id}", headers=h1).status_code == 200
    # contractors cannot see internal risk policies, audit data, or users
    for path in ("/api/v1/policies", "/api/v1/audit", "/api/v1/users", "/api/v1/decisions", "/api/v1/analytics/risk"):
        assert client.get(path, headers=h1).status_code == 403, path


def test_contractor_cannot_approve_own_contract_via_api(client, db, make_org):
    o = make_org()
    c = onboard(db, o, approve=False)
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == c.id)).scalars().first()
    h = login(client, o, "contractor")
    r = client.post(f"/api/v1/approvals/{ar.id}/decision", headers=h, json={"decision": "APPROVE", "reason": "self approval"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] in ("permission_denied", "separation_of_duties")


def test_auditor_is_read_only(client, db, make_org):
    o = make_org()
    c = onboard(db, o, approve=False)
    h = login(client, o, "auditor")
    assert client.get("/api/v1/audit", headers=h).status_code == 200
    assert client.get("/api/v1/audit/verify", headers=h).json()["valid"] is True
    assert client.get("/api/v1/audit/export", headers=h).status_code == 200
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == c.id)).scalars().first()
    assert client.post(f"/api/v1/approvals/{ar.id}/decision", headers=h,
                       json={"decision": "APPROVE", "reason": "auditor approving"}).status_code == 403
    assert client.post("/api/v1/policies", headers=h, json={"version": "x1", "config": {}, "note": "nope"}).status_code == 403


def test_admin_cannot_self_grant_financial_authority(client, db, make_org):
    o = make_org()
    h = login(client, o, "admin")
    r = client.put(f"/api/v1/users/{o.users['admin'].id}/roles", headers=h, json={"roles": ["SYSTEM_ADMIN", "FINANCE_MANAGER"]})
    assert r.status_code == 403
    r = client.put(f"/api/v1/users/{o.users['contractor'].id}/roles", headers=h, json={"roles": ["CONTRACTOR", "FINANCE_APPROVER"]})
    assert r.status_code == 422
    r = client.put(f"/api/v1/users/{o.users['analyst'].id}/roles", headers=h, json={"roles": ["AI_AGENT"]})
    assert r.status_code == 403
    # admin has no approval authority at all
    assert client.get("/api/v1/approvals", headers=h).status_code == 403


def test_refresh_rotation_and_reuse_detection(client, db, make_org):
    o = make_org()
    r = client.post("/api/v1/auth/login", json={"organization": o.slug, "email": o.users["analyst"].email, "password": PASSWORD})
    old_refresh = r.cookies.get("fg_refresh")
    csrf = r.cookies.get("fg_csrf")
    assert client.post("/api/v1/auth/refresh").status_code in (401, 403)  # missing CSRF header
    r2 = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200
    csrf = r2.cookies.get("fg_csrf")
    # replaying the rotated token (after the short concurrent-refresh grace window) revokes the whole session
    from datetime import timedelta

    from finguard.models import RefreshToken
    from finguard.core.security import hash_token

    row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(old_refresh))).scalar_one()
    row.rotated_at = row.rotated_at - timedelta(seconds=30)
    db.commit()
    client.cookies.set("fg_refresh", old_refresh, path="/api/v1/auth")
    r3 = client.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert r3.status_code == 401 and r3.json()["error"]["code"] == "token_reuse"
    token = r2.json()["access_token"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_login_lockout_after_failures(client, make_org):
    o = make_org()
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"organization": o.slug, "email": o.users["approver"].email, "password": "wrong-password"})
    r = client.post("/api/v1/auth/login", json={"organization": o.slug, "email": o.users["approver"].email, "password": PASSWORD})
    assert r.status_code == 401 and r.json()["error"]["code"] == "locked"


def test_upload_and_review_through_api(client, db, make_org):
    o = make_org()
    h = login(client, o, "contractor")
    r = client.post("/api/v1/contracts", headers=h, data={"title": "API contract"},
                    files={"file": ("c.pdf", contract_pdf(), "application/pdf")})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    ex = client.get(f"/api/v1/contracts/{cid}/extraction", headers=h).json()
    amount = next(f for f in ex["fields"] if f["field_name"] == "payment_amount")
    assert amount["value"] == "20000.00" and amount["source_page"] == 1
    page = client.get(f"/api/v1/documents/{r.json()['primary_document_id']}/pages/1", headers=h)
    assert page.status_code == 200 and page.headers["content-type"] == "image/png"
    assert client.post(f"/api/v1/contracts/{cid}/submit", headers=h).status_code == 200


def test_security_headers_and_error_shape(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "x-request-id" in r.headers
    r = client.get("/api/v1/contracts")
    assert set(r.json()["error"]) >= {"code", "message", "request_id"}


def test_failed_authorization_is_audited(client, db, make_org):
    o = make_org()
    h = login(client, o, "contractor")
    client.get("/api/v1/audit", headers=h)
    db.expire_all()
    assert db.execute(select(AuditLog).where(AuditLog.event_type == "authz.denied",
                                             AuditLog.actor_id == o.users["contractor"].id)).first()
