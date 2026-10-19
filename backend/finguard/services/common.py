"""Shared helpers: principal loading, service identities, calendars, contract-version terms."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz.principal import Principal
from finguard.core.errors import NotFound
from finguard.core.money import canonical_hash
from finguard.models import ContractVersion, Organization, Role, User, UserRole
from finguard.models.enums import RoleName, UserKind
from finguard.scheduling.generator import Calendar, Terms

SERVICE_EMAILS = {RoleName.SCHEDULER: "scheduler@service.finguard.internal",
                  RoleName.AI_AGENT: "agent@service.finguard.internal"}


def principal_for(db: Session, user: User, request_id: str | None = None, session_id: str | None = None,
                  on_behalf_of: Principal | None = None) -> Principal:
    roles = frozenset(ur.role.name for ur in user.roles)
    perms = frozenset(rp.permission.code for ur in user.roles for rp in ur.role.permissions)
    return Principal(user.id, user.org_id, roles, perms, user.kind, user.email, user.full_name, on_behalf_of,
                     request_id, session_id)


def service_principal(db: Session, org_id: uuid.UUID, role: RoleName, request_id: str | None = None,
                      on_behalf_of: Principal | None = None) -> Principal:
    user = db.execute(select(User).where(User.org_id == org_id, User.email == SERVICE_EMAILS[role],
                                         User.kind == UserKind.SERVICE)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise NotFound(f"service identity {role} not provisioned for organization")
    return principal_for(db, user, request_id, on_behalf_of=on_behalf_of)


def ensure_service_identities(db: Session, org: Organization) -> None:
    for role_name, email in SERVICE_EMAILS.items():
        exists = db.execute(select(User).where(User.org_id == org.id, User.email == email)).scalar_one_or_none()
        if exists:
            continue
        u = User(org_id=org.id, email=email, full_name=f"{role_name.title().replace('_', ' ')} service",
                 password_hash=None, kind=UserKind.SERVICE)
        db.add(u)
        db.flush()
        role = db.execute(select(Role).where(Role.name == role_name)).scalar_one()
        db.add(UserRole(user_id=u.id, role_id=role.id))
    db.flush()


def org_calendar(org: Organization) -> Calendar:
    st = org.settings or {}
    hol = frozenset(date.fromisoformat(d) for d in st.get("holidays", []))
    weekend = frozenset(int(x) for x in st.get("weekend", [5, 6]))
    return Calendar(hol, weekend, int(st.get("execution_hour", 9)))


def terms_of(v: ContractVersion) -> Terms:
    """Schedule terms for a version. Amended versions are anchored at their (period-aligned) effective date."""
    return Terms(payment_amount=Decimal(v.payment_amount), currency=v.currency, frequency=v.frequency,
                 start_date=v.effective_from or v.start_date, end_date=v.end_date, interval_count=v.interval_count,
                 custom_unit=v.custom_unit, due_day_of_month=v.due_day_of_month, due_weekday=v.due_weekday,
                 first_payment_date=v.first_payment_date if not v.effective_from else None,
                 payment_timing=v.payment_timing, business_day_rule=v.business_day_rule,
                 proration_rule=v.proration_rule, timezone=v.timezone)


TERM_FIELDS = ("contractor_legal_name", "counterparty_name", "total_value", "payment_amount", "currency", "frequency",
               "interval_count", "custom_unit", "due_day_of_month", "due_weekday", "start_date", "end_date",
               "first_payment_date", "payment_timing", "grace_period_days", "proration_rule", "business_day_rule",
               "timezone", "invoice_required", "auto_execute_permitted", "tax_withholding_note", "termination_terms",
               "notice_period_days", "effective_from")


def version_terms_dict(v: ContractVersion) -> dict:
    return {k: getattr(v, k) for k in TERM_FIELDS}


def terms_hash(d: dict) -> str:
    return canonical_hash({k: d.get(k) for k in TERM_FIELDS})
