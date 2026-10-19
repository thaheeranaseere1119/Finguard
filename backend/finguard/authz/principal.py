"""The authenticated actor of a request or job: a human user or a machine identity, scoped to one organization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from finguard.models.enums import UserKind


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID
    roles: frozenset[str]
    permissions: frozenset[str]
    kind: str = UserKind.HUMAN
    email: str = ""
    full_name: str = ""
    # For the AI agent acting on behalf of a user: the delegating human. Effective rights = agent ∩ delegator.
    on_behalf_of: "Principal | None" = None
    request_id: str | None = None
    session_id: str | None = None
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_service(self) -> bool:
        return self.kind == UserKind.SERVICE
