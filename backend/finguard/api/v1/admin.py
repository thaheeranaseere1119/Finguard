"""Administration: users & roles, permission matrix, policy versions, system health, evaluation runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finguard.api.deps import current_principal
from finguard.api.schemas import ActiveIn, PolicyIn, RolesIn, UserCreateIn
from finguard.api.serializers import user_view
from finguard.authz import abac
from finguard.authz.permissions import PERMISSIONS, ROLE_DESCRIPTIONS, ROLE_PERMISSIONS
from finguard.authz.principal import Principal
from finguard.core.config import get_settings
from finguard.core.db import get_db
from finguard.core.errors import Forbidden
from finguard.governance.service import activate_policy, create_policy_version
from finguard.models import EvaluationRun, JobHeartbeat, OcrJob, PolicyVersion, User
from finguard.models.enums import UserKind
from finguard.services import auth as auth_svc

router = APIRouter(tags=["admin"])


@router.get("/users")
def list_users(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    abac.require(p, "users:manage")
    rows = db.execute(select(User).where(User.org_id == p.org_id).order_by(User.kind, User.full_name)).scalars().all()
    return [user_view(u) for u in rows]


@router.post("/users", status_code=201)
def create_user(body: UserCreateIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    u = auth_svc.create_user(db, p, body.email, body.full_name, body.password, body.roles, body.company_name)
    db.commit()
    return user_view(u)


@router.put("/users/{user_id}/roles")
def set_roles(user_id: str, body: RolesIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    u = auth_svc.set_roles(db, p, user_id, body.roles)
    db.commit()
    return user_view(u)


@router.put("/users/{user_id}/active")
def set_active(user_id: str, body: ActiveIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    u = auth_svc.set_active(db, p, user_id, body.active)
    db.commit()
    return user_view(u)


@router.get("/roles")
def roles(p: Principal = Depends(current_principal)) -> dict:
    return {"permissions": PERMISSIONS,
            "roles": [{"name": r, "description": ROLE_DESCRIPTIONS[r], "permissions": sorted(ps)}
                      for r, ps in ROLE_PERMISSIONS.items()]}


@router.get("/policies")
def policies(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    abac.require(p, "policies:read")
    rows = db.execute(select(PolicyVersion).where(PolicyVersion.org_id == p.org_id)
                      .order_by(PolicyVersion.created_at.desc())).scalars().all()
    return [{"id": str(r.id), "version": r.version, "status": r.status, "config": r.config, "config_hash": r.config_hash,
             "change_note": r.change_note, "created_by": str(r.created_by), "created_at": r.created_at.isoformat()}
            for r in rows]


@router.post("/policies", status_code=201)
def new_policy(body: PolicyIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "policies:manage")
    pv = create_policy_version(db, p, body.version, body.config, body.note, body.activate)
    db.commit()
    return {"id": str(pv.id), "version": pv.version, "status": pv.status, "config_hash": pv.config_hash}


@router.post("/policies/{policy_id}/activate")
def activate(policy_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "policies:manage")
    pv = abac.load_scoped(db, PolicyVersion, policy_id, p)
    activate_policy(db, p, pv)
    db.commit()
    return {"id": str(pv.id), "status": pv.status}


@router.get("/system/health")
def system_health(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "system:read")
    s = get_settings()
    out: dict = {"environment": s.environment}
    try:
        db.execute(text("SELECT 1"))
        out["database"] = {"ok": True, "revision": db.execute(text("SELECT version_num FROM alembic_version")).scalar()}
    except Exception as exc:
        out["database"] = {"ok": False, "error": type(exc).__name__}
    try:
        from redis import Redis

        Redis.from_url(s.redis_url, socket_connect_timeout=0.5).ping()
        out["redis"] = {"ok": True}
    except Exception as exc:
        out["redis"] = {"ok": False, "error": type(exc).__name__}
    try:
        import os

        import pytesseract

        if s.tessdata_prefix:
            os.environ["TESSDATA_PREFIX"] = s.tessdata_prefix
        out["ocr"] = {"ok": True, "engine": f"tesseract {pytesseract.get_tesseract_version()}", "language": s.ocr_language}
    except Exception as exc:
        out["ocr"] = {"ok": False, "error": type(exc).__name__}
    out["llm"] = {"provider": s.llm_provider, "model": s.llm_model if s.llm_provider != "mock" else "mock-deterministic",
                  "configured": s.llm_provider == "mock" or bool({"gemini": s.gemini_api_key, "openai": s.openai_api_key,
                                                                   "anthropic": s.anthropic_api_key}.get(s.llm_provider))}
    out["payouts"] = {"provider": s.payout_provider, "mode": s.payout_mode,
                      "razorpayx_configured": all([s.razorpayx_key_id, s.razorpayx_key_secret, s.razorpayx_account_number,
                                                   s.razorpayx_webhook_secret]),
                      "dwolla_configured": bool(s.dwolla_key and s.dwolla_secret),
                      "dwolla_webhooks_configured": bool(s.dwolla_webhook_secret),
                      "live_money_enabled": s.payout_mode == "live"}
    now = datetime.now(timezone.utc)
    out["jobs"] = [{"name": h.name, "last_seen_at": h.last_seen_at.isoformat(),
                    "healthy": h.last_seen_at > now - timedelta(seconds=max(180, 3 * s.scheduler_tick_seconds)),
                    "last_result": h.last_result} for h in db.execute(select(JobHeartbeat)).scalars()]
    out["ocr_queue"] = dict(db.execute(select(OcrJob.status, func.count()).group_by(OcrJob.status)).all())
    out["users"] = dict(db.execute(select(User.kind, func.count()).where(User.org_id == p.org_id).group_by(User.kind)).all())
    return out


@router.post("/system/scheduler/tick")
def run_tick(p: Principal = Depends(current_principal)) -> dict:
    """Development/demo convenience: run one scheduler cycle now. Disabled in production."""
    abac.require(p, "system:read")
    if get_settings().environment == "production":
        raise Forbidden("manual scheduler ticks are disabled in production")
    from finguard.worker.jobs import scheduler_tick

    return scheduler_tick()


@router.get("/evaluation/runs")
def evaluation_runs(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    abac.require(p, "evaluation:read")
    rows = db.execute(select(EvaluationRun).order_by(EvaluationRun.created_at.desc()).limit(100)).scalars().all()
    return [{"id": str(r.id), "name": r.name, "kind": r.kind, "dataset_version": r.dataset_version, "split": r.split,
             "policy_config_hash": r.policy_config_hash, "metrics": r.metrics, "artifacts": r.artifacts,
             "is_simulated": r.is_simulated, "created_at": r.created_at.isoformat()} for r in rows]
