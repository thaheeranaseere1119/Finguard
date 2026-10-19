"""FastAPI dependencies: database session, authenticated principal, permission guards."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from finguard.authz.principal import Principal
from finguard.core.config import get_settings
from finguard.core.db import get_db
from finguard.core.errors import Forbidden, NotAuthenticated
from finguard.core.logging import request_id_var
from finguard.core.security import TokenError, TokenService
from finguard.models import User
from finguard.models.enums import UserKind
from finguard.services.audit import AuditService
from finguard.services.auth import session_active
from finguard.services.common import principal_for


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise NotAuthenticated("missing bearer token")
    try:
        claims = TokenService(get_settings()).decode_access(auth[7:].strip())
    except TokenError as exc:
        raise NotAuthenticated("invalid or expired token") from exc
    user = db.get(User, claims.user_id) if claims.user_id else None
    if user is None or not user.is_active or user.kind != UserKind.HUMAN or str(user.org_id) != claims.org_id:
        raise NotAuthenticated("account unavailable")
    if not session_active(db, claims.session_id):
        raise NotAuthenticated("session revoked")
    return principal_for(db, user, request_id=request_id_var.get(), session_id=claims.session_id)


def require_perm(*perms: str) -> Callable[..., Principal]:
    def dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not any(principal.has(p) for p in perms):
            AuditService.denied(principal, "api", f"missing permission {'|'.join(perms)}")
            raise Forbidden(f"requires permission: {' or '.join(perms)}", code="permission_denied")
        return principal

    return dep
