"""Background job entry points (run by the RQ worker or inline). Each opens its own session/transaction."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from finguard.core.db import new_session
from finguard.models import JobHeartbeat, OcrJob, Organization
from finguard.models.enums import OcrJobStatus, RoleName
from finguard.services.common import service_principal

log = logging.getLogger(__name__)


def _beat(name: str, result: dict) -> None:
    with new_session() as s:
        hb = s.get(JobHeartbeat, name) or JobHeartbeat(name=name, last_seen_at=datetime.now(timezone.utc))
        hb.last_seen_at, hb.last_result = datetime.now(timezone.utc), {k: str(v) for k, v in result.items()}
        s.merge(hb)
        s.commit()


def run_ocr_job(job_id: str) -> None:
    from finguard.models import Contract, ContractDocument
    from finguard.services.contracts import process_ocr_job

    db = new_session()
    try:
        job = db.get(OcrJob, uuid.UUID(job_id))
        if job is None:
            return
        org_id = db.get(Contract, db.get(ContractDocument, job.document_id).contract_id).org_id
        system = service_principal(db, org_id, RoleName.SCHEDULER)
        process_ocr_job(db, job.id, system)
    finally:
        db.close()


def requeue_stuck_ocr() -> int:
    """Recovery: OCR jobs that were QUEUED (lost message) or RUNNING for too long are re-run (max 3 attempts)."""
    from datetime import timedelta

    db = new_session()
    n = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        rows = db.execute(select(OcrJob).where(
            ((OcrJob.status == OcrJobStatus.QUEUED) & (OcrJob.created_at < cutoff)) |
            ((OcrJob.status == OcrJobStatus.RUNNING) & (OcrJob.started_at < cutoff)), OcrJob.attempts < 3)).scalars().all()
        ids = [r.id for r in rows]
    finally:
        db.close()
    for jid in ids:
        run_ocr_job(str(jid))
        n += 1
    return n


def process_due_payments() -> dict:
    from finguard.services.payments import process_due

    db = new_session()
    try:
        stats = process_due(db, worker_id=f"worker-{uuid.uuid4().hex[:6]}")
    finally:
        db.close()
    _beat("payments.process_due", stats)
    return stats


def reconcile_payments() -> int:
    from finguard.services.payments import reconcile_open

    db = new_session()
    try:
        n = reconcile_open(db)
    finally:
        db.close()
    _beat("payments.reconcile", {"reconciled": n})
    return n


def settle_mock_sandbox() -> int:
    """Mock provider only: settle pending sandbox payouts and deliver their signed webhooks through the real handler."""
    from finguard.payments.providers import get_payout_provider
    from finguard.payments.providers.mock import MockSandboxPaymentProvider
    from finguard.services.payments import handle_webhook

    p = get_payout_provider()
    if not isinstance(p, MockSandboxPaymentProvider):
        return 0
    n = 0
    for body, headers in p.settle_pending():
        db = new_session()
        try:
            handle_webhook(db, "mock", body, headers)
            n += 1
        finally:
            db.close()
    return n


def expire_contracts() -> int:
    from finguard.services.contracts import expire_contracts as _expire

    db = new_session()
    try:
        n = _expire(db, lambda org_id: service_principal(db, org_id, RoleName.SCHEDULER))
        db.commit()
    finally:
        db.close()
    return n


def scheduler_tick() -> dict:
    """One durable scheduling cycle. Safe to run concurrently (row locks with SKIP LOCKED) and after restarts."""
    result = {"ocr_recovered": requeue_stuck_ocr(), "expired": expire_contracts()}
    result.update(process_due_payments())
    result["reconciled"] = reconcile_payments()
    result["sandbox_webhooks"] = settle_mock_sandbox()
    _beat("scheduler.tick", result)
    return result


def ensure_orgs_have_identities() -> None:
    from finguard.services.common import ensure_service_identities

    with new_session() as s:
        for org in s.execute(select(Organization)).scalars():
            ensure_service_identities(s, org)
        s.commit()
