"""FastAPI application: middleware (request IDs, security headers, CORS allowlist), error mapping, routers."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from finguard.api.v1 import admin, auth, contracts, financials, governance, payments
from finguard.core.config import get_settings
from finguard.core.db import get_engine
from finguard.core.errors import AppError
from finguard.core.logging import configure_logging, request_id_var

log = logging.getLogger("finguard.api")


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level)
    app = FastAPI(title="FinGuard AI API", version="1.0.0",
                  description="Risk-tiered delegation for financial agents: ACT / APPROVE / ESCALATE / VERIFY / BLOCK.",
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        rid = "".join(ch for ch in rid if ch.isalnum() or ch in "-_")[:64] or uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        body = {"error": {"code": exc.code, "message": exc.message, "request_id": request_id_var.get()}}
        if exc.details is not None:
            body["error"]["details"] = exc.details
        return JSONResponse(body, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"error": {"code": "invalid_request", "message": "request validation failed",
                                       "details": details, "request_id": request_id_var.get()}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": {"code": "internal_error", "message": "an internal error occurred",
                                       "request_id": request_id_var.get()}}, status_code=500)

    for r in (auth.router, contracts.router, payments.router, governance.router, admin.router, financials.router):
        app.include_router(r, prefix="/api/v1")

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            return {"status": "ok", "payout_mode": s.payout_mode, "payout_provider": s.payout_provider}
        except Exception:
            return JSONResponse({"status": "degraded"}, status_code=503)  # type: ignore[return-value]

    return app


app = create_app()
