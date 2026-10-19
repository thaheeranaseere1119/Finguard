"""Authentication & session endpoints.

The refresh token is an HttpOnly, SameSite=Strict cookie scoped to /api/v1/auth. Refresh requires the CSRF
double-submit header (X-CSRF-Token == csrf cookie). The access token is returned in the body and sent as a Bearer
header (not a cookie), so state-changing API calls are not CSRF-exposed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from finguard.api.deps import current_principal
from finguard.api.schemas import LoginIn, RegisterIn
from finguard.api.serializers import user_view
from finguard.authz.principal import Principal
from finguard.core.config import get_settings
from finguard.core.db import get_db
from finguard.core.errors import Forbidden, NotAuthenticated, RateLimited
from finguard.core.ratelimit import hit
from finguard.core.security import new_csrf_token
from finguard.models import Organization, User
from finguard.services import auth as svc
from finguard.services.common import principal_for

router = APIRouter(prefix="/auth", tags=["auth"])
COOKIE_PATH = "/api/v1/auth"


def _set_cookies(resp: Response, refresh_token: str) -> None:
    s = get_settings()
    resp.set_cookie("fg_refresh", refresh_token, httponly=True, secure=s.cookie_secure, samesite="strict",
                    path=COOKIE_PATH, max_age=s.refresh_token_ttl_seconds)
    resp.set_cookie("fg_csrf", new_csrf_token(), httponly=False, secure=s.cookie_secure, samesite="strict",
                    path="/", max_age=s.refresh_token_ttl_seconds)


def _client(request: Request) -> tuple[str | None, str | None]:
    return request.headers.get("user-agent"), request.client.host if request.client else None


def _out(db: Session, user: User, tokens: dict) -> dict:
    p = principal_for(db, user)
    org = db.get(Organization, user.org_id)
    return {"access_token": tokens["access_token"], "token_type": "bearer", "expires_at": tokens["expires_at"],
            "user": user_view(user, p.permissions) | {"organization": {"name": org.name, "slug": org.slug,
                                                                         "base_currency": org.base_currency}}}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ua, ip = _client(request)
    if not hit(f"login:{ip}", 20, 60) or not hit(f"login:{body.organization}:{body.email.lower()}", 10, 300):
        raise RateLimited("too many login attempts")
    user, tokens = svc.login(db, body.organization, body.email, body.password, ua, ip)
    _set_cookies(response, tokens["refresh_token"])
    return _out(db, user, tokens)


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> dict:
    _, ip = _client(request)
    if not hit(f"register:{ip}", 5, 3600):
        raise RateLimited("too many registrations from this address")
    u = svc.register_contractor(db, body.organization, body.email, body.full_name, body.password, body.company_name)
    return {"id": str(u.id), "email": u.email, "roles": u.role_names}


@router.post("/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    token = request.cookies.get("fg_refresh")
    csrf_cookie, csrf_header = request.cookies.get("fg_csrf"), request.headers.get("x-csrf-token")
    if not token:
        raise NotAuthenticated("no refresh session")
    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise Forbidden("CSRF validation failed", code="csrf")
    ua, ip = _client(request)
    user, tokens = svc.refresh(db, token, ua, ip)
    _set_cookies(response, tokens["refresh_token"])
    return _out(db, user, tokens)


@router.post("/logout")
def logout(response: Response, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    svc.logout(db, p)
    db.commit()
    response.delete_cookie("fg_refresh", path=COOKIE_PATH)
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, p.user_id)
    org = db.get(Organization, p.org_id)
    return user_view(user, p.permissions) | {"organization": {"name": org.name, "slug": org.slug,
                                                              "base_currency": org.base_currency}}


@router.get("/sessions")
def sessions(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    return svc.list_sessions(db, p)


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    svc.logout(db, p, session_id)
    db.commit()
    return {"ok": True}
