from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Numeric, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from finguard.models.enums import values

Money = Numeric(18, 2)
Score = Numeric(6, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())


def enum_check(column: str, enum: type[StrEnum], name: str) -> CheckConstraint:
    allowed = ",".join(f"'{v}'" for v in values(enum))
    return CheckConstraint(f"{column} IN ({allowed})", name=name)
