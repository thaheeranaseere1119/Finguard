"""Test fixtures. Tests run against a real PostgreSQL test database (triggers, partial unique indexes and row locks
are part of what is being tested). No external credentials are needed: the LLM and payout providers are the
deterministic mocks.

    TEST_DATABASE_URL=postgresql+psycopg://finguard:finguard_dev_only@localhost:5432/finguard_test pytest
"""

from __future__ import annotations

import os
import tempfile
import uuid

_STORAGE = tempfile.mkdtemp(prefix="finguard-test-")
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL",
                                   "postgresql+psycopg://finguard:finguard_dev_only@localhost:5432/finguard_test"),
    "USE_JOB_QUEUE": "false",
    "LLM_PROVIDER": "mock",
    "PAYOUT_PROVIDER": "mock",
    "PAYOUT_MODE": "sandbox",
    "STORAGE_DIR": _STORAGE,
    "JWT_SECRET": "test-secret-" + "x" * 40,
    "SCHEDULER_LEASE_SECONDS": "60",
})
os.environ.setdefault("TESSDATA_PREFIX", "/opt/anaconda3/share/tessdata")
if os.path.isdir(os.environ["TESSDATA_PREFIX"]) is False:
    os.environ.pop("TESSDATA_PREFIX")

from dataclasses import dataclass  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from dateutil.relativedelta import relativedelta  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from finguard.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from finguard.core.db import get_engine, new_session  # noqa: E402

PASSWORD = "Test-Password-123!"


@pytest.fixture(scope="session", autouse=True)
def database():
    from alembic import command
    from alembic.config import Config

    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")
    from finguard.cli import bootstrap

    with new_session() as s:
        bootstrap(s)
        s.commit()
    yield


@pytest.fixture
def db():
    s = new_session()
    yield s
    s.rollback()
    s.close()


@dataclass
class Org:
    org: object
    slug: str
    users: dict

    def p(self, db, key: str):
        from finguard.services.common import principal_for

        db.expire_all()
        from finguard.models import User

        return principal_for(db, db.get(User, self.users[key].id))

    def scheduler(self, db):
        from finguard.models.enums import RoleName
        from finguard.services.common import service_principal

        return service_principal(db, self.org.id, RoleName.SCHEDULER)


@pytest.fixture
def make_org(db):
    from finguard.cli import create_org, create_user
    from finguard.models.enums import RoleName as R

    def _make(prefix: str = "org") -> Org:
        slug = f"{prefix}-{uuid.uuid4().hex[:8]}"
        org = create_org(db, slug, f"{prefix.title()} Ltd", f"admin@{slug}.test", PASSWORD)
        from finguard.models import User

        users = {"admin": db.execute(select(User).where(User.email == f"admin@{slug}.test")).scalar_one()}
        for key, roles, company in [("contractor", [R.CONTRACTOR], "Raman Data Labs"), ("contractor2", [R.CONTRACTOR], None),
                                    ("analyst", [R.FINANCE_ANALYST], None), ("approver", [R.FINANCE_APPROVER], None),
                                    ("approver2", [R.FINANCE_APPROVER], None), ("manager", [R.FINANCE_MANAGER], None),
                                    ("risk", [R.RISK_OFFICER], None), ("auditor", [R.AUDITOR], None)]:
            name = "Priya Raman" if key == "contractor" else f"{key.title()} User"
            users[key] = create_user(db, org, f"{key}@{slug}.test", name, PASSWORD, roles, company)
        db.commit()
        return Org(org, slug, users)

    return _make


def run_ocr(db, contract_id):
    """OCR already ran inline after commit (USE_JOB_QUEUE=false); refresh the session view."""
    db.expire_all()


def contract_pdf(**kw) -> bytes:
    from finguard.samples import ContractSpec, contract_lines, searchable_pdf

    return searchable_pdf(contract_lines(spec(**kw)))


def spec(**kw):
    from finguard.samples import ContractSpec

    today = date.today().replace(day=1)
    base = dict(contract_no=f"T-{uuid.uuid4().hex[:6].upper()}", company="Acme Analytics Pvt Ltd", contractor="Priya Raman",
                amount=20000, start=today - relativedelta(months=3), end=today + relativedelta(months=9) - timedelta(days=1),
                auto_pay=True)
    base.update(kw)
    return ContractSpec(**base)


def onboard(db, o: Org, *, account="50100000010000", data: bytes | None = None, approve=True, approver="approver",
            ifsc_override: str | None = None, **kw):
    """Drive a contract through upload → OCR → submit → validation → (approval) using real services."""
    from finguard.models import ApprovalRequest, Contract
    from finguard.services import approvals, beneficiaries, contracts

    cp = o.p(db, "contractor")
    c = contracts.create_with_document(db, cp, "Test contract", "contract.pdf", data or contract_pdf(**kw))
    db.commit()
    run_ocr(db, c.id)
    cid = c.id
    beneficiaries.register_beneficiary(db, cp, cid, "Priya Raman", account, ifsc_override or "HDFC0001234")
    db.commit()
    contracts.submit(db, o.p(db, "contractor"), cid)
    db.commit()
    contracts.validate(db, o.p(db, "analyst"), cid, "PASSED", "Checked against source document")
    db.commit()
    c = db.get(Contract, cid)
    beneficiaries.verify_beneficiary(db, o.p(db, "analyst"), c.beneficiary_id, True, "verified")
    db.commit()
    if approve:
        ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == cid,
                                                      ApprovalRequest.status == "OPEN")).scalars().first()
        approvals.decide(db, o.p(db, approver), ar.id, "APPROVE", "Within budget and policy")
        db.commit()
    db.expire_all()
    return db.get(Contract, cid)


def drain(db, at, worker="test-worker"):
    """Run scheduler cycles until nothing more is claimable at time `at` (other tests may leave due payments)."""
    from finguard.services import payments

    total = {}
    for _ in range(50):
        stats = payments.process_due(db, at, worker)
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
        if stats.get("claimed", 0) == 0:
            break
    db.expire_all()
    return total


@pytest.fixture
def now():
    return datetime.now(timezone.utc)
