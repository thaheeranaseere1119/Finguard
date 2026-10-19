"""Resource-level authorization (ABAC): tenant isolation, ownership, separation of duties, approval limits.

Every check raises ``Forbidden``/``NotFound`` and is called by services, not only routers, so direct service use
(agent tools, workers) is protected too. Cross-tenant lookups return 404 to avoid leaking existence.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finguard.authz.principal import Principal
from finguard.core.errors import Forbidden, NotFound


def _audit_denial(principal: Principal, reason: str) -> None:
    from finguard.services.audit import AuditService

    AuditService.denied(principal, "permission_check", reason)


def require(principal: Principal, *permissions: str) -> None:
    missing = [p for p in permissions if not principal.has(p)]
    if missing:
        _audit_denial(principal, f"missing permission: {', '.join(missing)}")
        raise Forbidden(f"missing permission: {', '.join(missing)}", code="permission_denied")


def require_any(principal: Principal, *permissions: str) -> str:
    for p in permissions:
        if principal.has(p):
            return p
    _audit_denial(principal, f"requires one of: {', '.join(permissions)}")
    raise Forbidden(f"requires one of: {', '.join(permissions)}", code="permission_denied")


def ensure_same_org(principal: Principal, org_id: uuid.UUID | None) -> None:
    if org_id is None or org_id != principal.org_id:
        raise NotFound("resource not found")


def load_scoped(db: Session, model: Any, resource_id: uuid.UUID | str, principal: Principal) -> Any:
    """Load a row by id and enforce organization scope (404 across tenants)."""
    try:
        rid = resource_id if isinstance(resource_id, uuid.UUID) else uuid.UUID(str(resource_id))
    except ValueError as exc:
        raise NotFound("resource not found") from exc
    obj = db.get(model, rid)
    if obj is None:
        raise NotFound("resource not found")
    ensure_same_org(principal, getattr(obj, "org_id", None))
    return obj


def can_read_contract(principal: Principal, contract: Any) -> bool:
    if contract.org_id != principal.org_id:
        return False
    if principal.has("contracts:read_org"):
        return True
    return principal.has("contracts:read_own") and contract.contractor_id == principal.user_id


def ensure_can_read_contract(principal: Principal, contract: Any) -> None:
    if contract.org_id != principal.org_id:
        raise NotFound("resource not found")
    if not can_read_contract(principal, contract):
        raise NotFound("resource not found")


def ensure_owner(principal: Principal, contract: Any) -> None:
    if contract.org_id != principal.org_id or contract.contractor_id != principal.user_id:
        raise Forbidden("only the contractor who owns this contract may do this", code="not_owner")


def ensure_not_self(principal: Principal, *other_user_ids: uuid.UUID | None, what: str) -> None:
    """Separation of duties: the actor must not be the requester/contractor/earlier decision-maker."""
    if principal.user_id in {u for u in other_user_ids if u is not None}:
        raise Forbidden(f"separation of duties: you cannot {what}", code="separation_of_duties")


def approval_limit_for(roles: frozenset[str], limits: dict[str, str]) -> Decimal:
    """Highest configured limit among the actor's roles (in the organization's base currency)."""
    best = Decimal("0")
    for role in roles:
        if role in limits:
            best = max(best, Decimal(str(limits[role])))
    return best


def ensure_within_limit(principal: Principal, amount_base: Decimal, limits: dict[str, str], what: str) -> None:
    limit = approval_limit_for(principal.roles, limits)
    if amount_base > limit:
        raise Forbidden(f"amount {amount_base} exceeds your approval limit {limit} for {what}", code="approval_limit")
