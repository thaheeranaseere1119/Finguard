"""In-app notifications. Delivered to users by role within an organization, or to a specific user."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.models import Notification, Role, User, UserRole


def notify_user(db: Session, org_id: uuid.UUID, user_id: uuid.UUID, kind: str, title: str, body: str = "",
                link: str | None = None) -> None:
    db.add(Notification(org_id=org_id, user_id=user_id, kind=kind, title=title[:200], body=body, link=link))


def notify_roles(db: Session, org_id: uuid.UUID, roles: Iterable[str], kind: str, title: str, body: str = "",
                 link: str | None = None, exclude: Iterable[uuid.UUID] = ()) -> int:
    excluded = set(exclude)
    q = (select(User.id).join(UserRole, UserRole.user_id == User.id).join(Role, Role.id == UserRole.role_id)
         .where(User.org_id == org_id, User.is_active.is_(True), Role.name.in_(list(roles))).distinct())
    n = 0
    for uid in db.execute(q).scalars():
        if uid in excluded:
            continue
        notify_user(db, org_id, uid, kind, title, body, link)
        n += 1
    return n
