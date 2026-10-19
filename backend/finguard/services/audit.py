"""Tamper-evident audit log (SHA-256 hash chain), written in the same transaction as the business change.

entry_hash_n = SHA256(canonical({seq, created_at, event_type, actor, org, resource, outcome, states, payload, prev_hash}))
Rows are append-only (DB trigger blocks UPDATE/DELETE). ``verify_chain`` recomputes the chain end to end.
Payloads are recursively redacted before storage. Adapted from Finguard-v3.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz.principal import Principal
from finguard.core.logging import request_id_var
from finguard.core.money import canonical_hash, to_jsonable
from finguard.core.redact import redact
from finguard.models import AuditChainHead, AuditLog

GENESIS = "0" * 64


def _hash_material(row: AuditLog) -> dict[str, Any]:
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)  # the DB session may return local time; hash the UTC instant
    return {
        "seq": row.seq, "created_at": created.isoformat(), "event_type": row.event_type,
        "actor_id": str(row.actor_id) if row.actor_id else None, "org_id": str(row.org_id) if row.org_id else None,
        "resource_type": row.resource_type, "resource_id": row.resource_id,
        "contract_id": str(row.contract_id) if row.contract_id else None, "outcome": row.outcome,
        "previous_state": row.previous_state, "new_state": row.new_state, "request_id": row.request_id,
        "payload": row.payload, "prev_hash": row.prev_hash,
    }


class AuditService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _head(self) -> AuditChainHead:
        head = self.db.execute(select(AuditChainHead).where(AuditChainHead.id == 1).with_for_update()).scalar_one_or_none()
        if head is None:
            head = AuditChainHead(id=1, seq=0, last_hash=GENESIS)
            self.db.add(head)
            self.db.flush()
        return head

    def record(self, event_type: str, *, actor: Principal | None = None, org_id: uuid.UUID | None = None,
               resource_type: str | None = None, resource_id: Any = None, contract_id: uuid.UUID | None = None,
               outcome: str = "SUCCESS", previous_state: str | None = None, new_state: str | None = None,
               payload: dict[str, Any] | None = None) -> AuditLog:
        head = self._head()
        data = to_jsonable(redact(dict(payload or {})))
        if actor is not None and actor.on_behalf_of is not None:
            data["on_behalf_of"] = str(actor.on_behalf_of.user_id)
        row = AuditLog(
            id=uuid.uuid4(), seq=head.seq + 1, created_at=datetime.now(timezone.utc), event_type=event_type,
            org_id=org_id or (actor.org_id if actor else None), actor_id=actor.user_id if actor else None,
            actor_roles=sorted(actor.roles) if actor else [], actor_kind=actor.kind if actor else None,
            resource_type=resource_type, resource_id=str(resource_id) if resource_id is not None else None,
            contract_id=contract_id, outcome=outcome, previous_state=previous_state, new_state=new_state,
            request_id=(actor.request_id if actor and actor.request_id else request_id_var.get()),
            payload=data, prev_hash=head.last_hash, entry_hash="",
        )
        row.entry_hash = canonical_hash(_hash_material(row))
        self.db.add(row)
        head.seq = row.seq
        head.last_hash = row.entry_hash
        self.db.flush()
        return row

    @staticmethod
    def denied(actor: Principal | None, action: str, reason: str, **kw: Any) -> None:
        """Record a failed authorization attempt. Uses its own short transaction so it survives the request rollback."""
        from finguard.core.db import new_session

        s = new_session()
        try:
            AuditService(s).record("authz.denied", actor=actor, outcome="DENIED",
                                   payload={"action": action, "reason": reason, **kw.pop("payload", {})}, **kw)
            s.commit()
        except Exception:  # never let audit-of-denial failures mask the original 403
            s.rollback()
        finally:
            s.close()

    def verify_chain(self) -> dict[str, Any]:
        prev = GENESIS
        n = 0
        for row in self.db.execute(select(AuditLog).order_by(AuditLog.seq)).scalars():
            n += 1
            if row.prev_hash != prev or row.entry_hash != canonical_hash(_hash_material(row)) or row.seq != n:
                return {"valid": False, "checked": n, "broken_at_seq": row.seq}
            prev = row.entry_hash
        return {"valid": True, "checked": n, "head": prev}
