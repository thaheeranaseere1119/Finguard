"""Background job dispatch.

Jobs are enqueued only AFTER the current transaction commits (so a worker never sees uncommitted rows). With
USE_JOB_QUEUE=true they go to Redis/RQ and run in the `worker` process; otherwise they run inline right after the
commit (tests and single-process demos). Job state itself is durable in PostgreSQL (ocr_jobs, scheduled_payments,
payment_attempts), so a lost queue message or a worker crash is recovered by the periodic scheduler tick.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from finguard.core.config import get_settings

log = logging.getLogger(__name__)
QUEUE_NAME = "finguard"


def _run(path: str, args: tuple[Any, ...]) -> None:
    mod, fn = path.rsplit(".", 1)
    getattr(importlib.import_module(mod), fn)(*args)


def _enqueue_now(path: str, args: tuple[Any, ...]) -> None:
    s = get_settings()
    if s.use_job_queue:
        try:
            from redis import Redis
            from rq import Queue

            Queue(QUEUE_NAME, connection=Redis.from_url(s.redis_url)).enqueue(path, *args, job_timeout=600)
            return
        except Exception as exc:  # Redis down: the durable DB state lets the scheduler tick pick it up later
            log.warning("queue unavailable, job %s deferred to scheduler recovery: %s", path, type(exc).__name__)
            return
    try:
        _run(path, args)
    except Exception:
        log.exception("inline job %s failed", path)


def after_commit(db: Session, path: str, *args: Any) -> None:
    pending = db.info.setdefault("pending_jobs", [])
    pending.append((path, tuple(str(a) for a in args)))
    if not db.info.get("dispatch_hooked"):
        db.info["dispatch_hooked"] = True

        @event.listens_for(db, "after_commit")
        def _go(session: Session) -> None:
            jobs = session.info.pop("pending_jobs", [])
            for p, a in jobs:
                _enqueue_now(p, a)

        @event.listens_for(db, "after_rollback")
        def _drop(session: Session) -> None:
            session.info.pop("pending_jobs", None)
