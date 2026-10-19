"""Pydantic request/response schemas. Responses are built from explicit serializers, never from ORM objects directly."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Syntactic email check (EmailStr rejects reserved TLDs such as .test used by demo/test organizations).
EMAIL = r"^[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginIn(Strict):
    organization: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterIn(Strict):
    organization: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=254, pattern=EMAIL)
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=12, max_length=256)
    company_name: str | None = Field(default=None, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: dict[str, Any]


class UserCreateIn(Strict):
    email: str = Field(max_length=254, pattern=EMAIL)
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=12, max_length=256)
    roles: list[str] = Field(min_length=1)
    company_name: str | None = None


class RolesIn(Strict):
    roles: list[str] = Field(min_length=1)


class ActiveIn(Strict):
    active: bool


class FieldCorrectionIn(Strict):
    field_name: str = Field(max_length=64)
    value: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=3, max_length=1000)


class ValidationIn(Strict):
    decision: Literal["PASSED", "FAILED", "CHANGES_REQUESTED"]
    reason: str = Field(min_length=3, max_length=2000)


class HoldIn(Strict):
    action: Literal["SUSPEND", "REACTIVATE", "TERMINATE"]
    reason: str = Field(min_length=3, max_length=1000)


class BeneficiaryIn(Strict):
    account_holder_name: str = Field(min_length=3, max_length=120)
    account_number: str = Field(min_length=5, max_length=40)
    ifsc: str = Field(min_length=11, max_length=11)


class VerifyIn(Strict):
    approve: bool
    note: str = Field(min_length=3, max_length=500)


class AmendmentIn(Strict):
    changes: dict[str, str] = Field(min_length=1)
    reason: str = Field(min_length=5, max_length=2000)
    effective_date: date


class InvoiceIn(Strict):
    invoice_number: str = Field(min_length=1, max_length=64)
    amount: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=1, max_length=8)


class ApprovalDecisionIn(Strict):
    decision: Literal["APPROVE", "REJECT", "CLEAR", "HOLD"]
    reason: str = Field(min_length=5, max_length=2000)


class AgentIn(Strict):
    message: str = Field(min_length=1, max_length=2000)


class PolicyIn(Strict):
    version: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9._\-]+$")
    config: dict[str, Any]
    note: str = Field(min_length=3, max_length=1000)
    activate: bool = False


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
