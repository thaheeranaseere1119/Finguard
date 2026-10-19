"""Authentication, sessions (rotating refresh tokens with reuse detection) and user/role administration."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.permissions import FINANCIAL_AUTHORITY_ROLES, SERVICE_ROLES
from finguard.authz.principal import Principal
from finguard.core.config import get_settings
from finguard.core.errors import Conflict, Forbidden, NotAuthenticated, NotFound, ValidationFailed
from finguard.core.security import TokenService, hash_password, hash_token, new_refresh_token, validate_password_policy, verify_password
from finguard.models import Organization, RefreshToken, Role, User, UserRole
from finguard.models.enums import RoleName, UserKind
from finguard.services.audit import AuditService

MAX_FAILED = 5
REFRESH_RACE_SECONDS = 5
LOCK_MINUTES = 15
CONFLICTING_ROLES = {RoleName.CONTRACTOR} | {RoleName.FINANCE_APPROVER, RoleName.FINANCE_MANAGER, RoleName.FINANCE_ANALYST,
                                             RoleName.RISK_OFFICER}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue(db: Session, user: User, session_id: uuid.UUID, ua: str | None, ip: str | None) -> dict:
    s = get_settings()
    access, exp = TokenService(s).issue_access(user.id, user.org_id, session_id)
    refresh, h = new_refresh_token()
    db.add(RefreshToken(session_id=session_id, user_id=user.id, token_hash=h, user_agent=(ua or "")[:300], ip_address=ip,
                        expires_at=_now() + timedelta(seconds=s.refresh_token_ttl_seconds)))
    return {"access_token": access, "token_type": "bearer", "expires_at": exp.isoformat(), "refresh_token": refresh,
            "session_id": str(session_id)}


def login(db: Session, org_slug: str, email: str, password: str, ua: str | None, ip: str | None) -> tuple[User, dict]:
    org = db.execute(select(Organization).where(Organization.slug == org_slug.strip().lower())).scalar_one_or_none()
    user = None
    if org:
        user = db.execute(select(User).where(User.org_id == org.id, User.email == email.strip().lower())).scalar_one_or_none()
    if user and user.locked_until and user.locked_until > _now():
        verify_password(password, None)
        AuditService(db).record("auth.login_locked", org_id=user.org_id, outcome="DENIED", resource_type="user",
                                resource_id=user.id, payload={"ip": ip})
        db.commit()
        raise NotAuthenticated("account temporarily locked; try again later", code="locked")
    ok = verify_password(password, user.password_hash if user else None)
    if not ok or not user or not user.is_active or user.kind != UserKind.HUMAN:
        if user:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED:
                user.locked_until = _now() + timedelta(minutes=LOCK_MINUTES)
                user.failed_logins = 0
            AuditService(db).record("auth.login_failed", org_id=user.org_id, outcome="DENIED", resource_type="user",
                                    resource_id=user.id, payload={"ip": ip})
            db.commit()
        raise NotAuthenticated("invalid credentials", code="invalid_credentials")
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, _now()
    tokens = _issue(db, user, uuid.uuid4(), ua, ip)
    AuditService(db).record("auth.login", org_id=user.org_id, actor=None, resource_type="user", resource_id=user.id,
                            payload={"ip": ip, "session_id": tokens["session_id"]})
    db.commit()
    return user, tokens


def refresh(db: Session, token: str, ua: str | None, ip: str | None) -> tuple[User, dict]:
    row = db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(token)).with_for_update()).scalar_one_or_none()
    if row is None:
        raise NotAuthenticated("invalid refresh token")
    if row.rotated_at is not None and row.revoked_at is None and _now() - row.rotated_at < timedelta(seconds=REFRESH_RACE_SECONDS):
        # Concurrent refresh from the same client (e.g. two tabs): reject this one without revoking the session.
        raise NotAuthenticated("refresh already in progress; retry", code="refresh_race")
    if row.rotated_at is not None or row.revoked_at is not None:
        # Reuse of a rotated token ⇒ likely theft: revoke the whole session.
        for t in db.execute(select(RefreshToken).where(RefreshToken.session_id == row.session_id)).scalars():
            t.revoked_at = t.revoked_at or _now()
        AuditService(db).record("auth.refresh_reuse_detected", outcome="DENIED", resource_type="session",
                                resource_id=row.session_id, payload={"ip": ip})
        db.commit()
        raise NotAuthenticated("refresh token reuse detected; session revoked", code="token_reuse")
    if row.expires_at < _now():
        raise NotAuthenticated("refresh token expired")
    user = db.get(User, row.user_id)
    if not user or not user.is_active:
        raise NotAuthenticated("account disabled")
    row.rotated_at = _now()
    tokens = _issue(db, user, row.session_id, ua, ip)
    db.commit()
    return user, tokens


def session_active(db: Session, session_id: str) -> bool:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        return False
    return db.execute(select(RefreshToken.id).where(RefreshToken.session_id == sid, RefreshToken.revoked_at.is_(None),
                                                    RefreshToken.expires_at > _now())).first() is not None


def logout(db: Session, actor: Principal, session_id: str | None = None) -> None:
    sid = uuid.UUID(session_id or actor.session_id)
    rows = db.execute(select(RefreshToken).where(RefreshToken.session_id == sid, RefreshToken.user_id == actor.user_id)).scalars().all()
    if not rows:
        raise NotFound("session not found")
    for t in rows:
        t.revoked_at = t.revoked_at or _now()
    AuditService(db).record("auth.logout", actor=actor, resource_type="session", resource_id=sid)


def list_sessions(db: Session, actor: Principal) -> list[dict]:
    rows = db.execute(select(RefreshToken).where(RefreshToken.user_id == actor.user_id, RefreshToken.revoked_at.is_(None),
                                                 RefreshToken.rotated_at.is_(None), RefreshToken.expires_at > _now())
                      .order_by(RefreshToken.created_at.desc())).scalars().all()
    return [{"session_id": str(r.session_id), "created_at": r.created_at.isoformat(), "user_agent": r.user_agent,
             "ip_address": r.ip_address, "current": str(r.session_id) == actor.session_id} for r in rows]


def register_contractor(db: Session, org_slug: str, email: str, full_name: str, password: str,
                        company_name: str | None) -> User:
    org = db.execute(select(Organization).where(Organization.slug == org_slug.strip().lower())).scalar_one_or_none()
    if org is None or not org.allow_self_registration:
        raise ValidationFailed("organization not found or not accepting registrations")
    problems = validate_password_policy(password, get_settings().password_min_length)
    if problems:
        raise ValidationFailed("password " + "; ".join(problems))
    email = email.strip().lower()
    if db.execute(select(User.id).where(User.org_id == org.id, User.email == email)).first():
        raise Conflict("an account with this email already exists")
    u = User(org_id=org.id, email=email, full_name=full_name.strip()[:200], password_hash=hash_password(password),
             company_name=(company_name or "").strip()[:200] or None)
    db.add(u)
    db.flush()
    role = db.execute(select(Role).where(Role.name == RoleName.CONTRACTOR)).scalar_one()
    db.add(UserRole(user_id=u.id, role_id=role.id))
    AuditService(db).record("user.registered", org_id=org.id, resource_type="user", resource_id=u.id,
                            payload={"email": email, "role": RoleName.CONTRACTOR})
    db.commit()
    return u


def create_user(db: Session, actor: Principal, email: str, full_name: str, password: str, roles: list[str],
                company_name: str | None = None) -> User:
    abac.require(actor, "users:manage")
    problems = validate_password_policy(password, get_settings().password_min_length)
    if problems:
        raise ValidationFailed("password " + "; ".join(problems))
    email = email.strip().lower()
    if db.execute(select(User.id).where(User.org_id == actor.org_id, User.email == email)).first():
        raise Conflict("an account with this email already exists")
    u = User(org_id=actor.org_id, email=email, full_name=full_name.strip(), password_hash=hash_password(password),
             company_name=company_name)
    db.add(u)
    db.flush()
    set_roles(db, actor, u.id, roles)
    AuditService(db).record("user.created", actor=actor, resource_type="user", resource_id=u.id, payload={"email": email})
    return u


def set_roles(db: Session, actor: Principal, user_id: uuid.UUID | str, roles: list[str]) -> User:
    abac.require(actor, "users:manage")
    u = abac.load_scoped(db, User, user_id, actor)
    wanted = set(roles)
    if wanted & SERVICE_ROLES or u.kind == UserKind.SERVICE:
        raise Forbidden("service identities and service roles cannot be assigned through user administration")
    if u.id == actor.user_id and wanted & FINANCIAL_AUTHORITY_ROLES:
        raise Forbidden("administrators cannot grant themselves financial approval authority", code="separation_of_duties")
    if RoleName.CONTRACTOR in wanted and wanted & (CONFLICTING_ROLES - {RoleName.CONTRACTOR}):
        raise ValidationFailed("a contractor cannot also hold an internal finance or risk role (separation of duties)")
    found = {r.name: r for r in db.execute(select(Role).where(Role.name.in_(wanted))).scalars()}
    if set(found) != wanted:
        raise ValidationFailed(f"unknown roles: {', '.join(sorted(wanted - set(found)))}")
    before = sorted(u.role_names)
    u.roles.clear()
    db.flush()
    for name in sorted(wanted):
        u.roles.append(UserRole(user_id=u.id, role_id=found[name].id, granted_by=actor.user_id))
    db.flush()
    AuditService(db).record("user.roles_changed", actor=actor, resource_type="user", resource_id=u.id,
                            previous_state=",".join(before)[:40], new_state=",".join(sorted(wanted))[:40],
                            payload={"before": before, "after": sorted(wanted)})
    return u


def set_active(db: Session, actor: Principal, user_id: str, active: bool) -> User:
    abac.require(actor, "users:manage")
    u = abac.load_scoped(db, User, user_id, actor)
    if u.id == actor.user_id:
        raise Forbidden("you cannot deactivate yourself")
    u.is_active = active
    if not active:
        for t in db.execute(select(RefreshToken).where(RefreshToken.user_id == u.id, RefreshToken.revoked_at.is_(None))).scalars():
            t.revoked_at = _now()
    AuditService(db).record("user.activation_changed", actor=actor, resource_type="user", resource_id=u.id,
                            new_state="ACTIVE" if active else "INACTIVE")
    return u
