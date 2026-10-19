"""Application settings, loaded from environment variables (12-factor). No secret has a usable default."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    app_name: str = "FinGuard AI"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://finguard:finguard_dev_only@localhost:5432/finguard"
    redis_url: str = "redis://localhost:6379/0"
    # When false, jobs run inline in the request (tests, single-process demos). The worker is still the production path.
    use_job_queue: bool = True

    jwt_secret: str = Field(default="", repr=False)
    jwt_issuer: str = "finguard-ai"
    jwt_audience: str = "finguard-api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 7 * 24 * 3600
    password_min_length: int = 12
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    storage_dir: Path = BACKEND_ROOT / "storage"
    max_upload_bytes: int = 15 * 1024 * 1024
    max_pdf_pages: int = 60
    tessdata_prefix: str | None = None
    ocr_language: str = "eng"
    ocr_dpi: int = 300
    # Pages with fewer embedded characters than this are treated as scanned and OCR'd.
    native_text_min_chars: int = 40

    # LLM provider for extraction and the agent: "mock" (deterministic, offline) | "gemini" | "openai" | "anthropic"
    llm_provider: str = "mock"
    llm_model: str = "gemini-2.5-flash"
    llm_timeout_seconds: float = 30.0
    gemini_api_key: str = Field(default="", repr=False)
    openai_api_key: str = Field(default="", repr=False)
    anthropic_api_key: str = Field(default="", repr=False)
    groq_api_key: str = Field(default="", repr=False)

    # Payout provider. "mock" is the deterministic sandbox simulator. "razorpayx" / "dwolla" need sandbox credentials.
    payout_provider: Literal["mock", "razorpayx", "dwolla"] = "mock"
    payout_mode: Literal["sandbox", "live"] = "sandbox"
    # Live payouts need BOTH payout_mode=live AND this exact acknowledgement string AND environment=production.
    live_payouts_acknowledgement: str = Field(default="", repr=False)
    mock_webhook_secret: str = Field(default="mock-sandbox-webhook-secret-not-for-production", repr=False)
    razorpayx_key_id: str = Field(default="", repr=False)
    razorpayx_key_secret: str = Field(default="", repr=False)
    razorpayx_account_number: str = Field(default="", repr=False)
    razorpayx_webhook_secret: str = Field(default="", repr=False)
    razorpayx_base_url: str = "https://api.razorpay.com"
    # Dwolla (US ACH, USD). Sandbox URL is used unless PAYOUT_MODE=live.
    dwolla_key: str = Field(default="", repr=False)
    dwolla_secret: str = Field(default="", repr=False)
    dwolla_webhook_secret: str = Field(default="", repr=False)
    dwolla_funding_source: str = ""      # optional: id/URL of the funding source to pay from (default: verified bank)
    dwolla_base_url: str = ""
    payout_timeout_seconds: float = 15.0

    scheduler_tick_seconds: int = 60
    scheduler_lease_seconds: int = 300

    @model_validator(mode="after")
    def _safety(self) -> "Settings":
        if self.environment == "production":
            if len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must be set to at least 32 characters in production")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production")
            if self.payout_provider == "mock" and self.payout_mode == "live":
                raise ValueError("The mock provider cannot run in live mode")
        elif not self.jwt_secret:
            # Development/test only: a process-local random secret (tokens do not survive restarts).
            import secrets

            self.jwt_secret = secrets.token_urlsafe(48)
        if self.payout_mode == "live":
            if self.environment != "production" or self.live_payouts_acknowledgement != "I_UNDERSTAND_REAL_MONEY_WILL_MOVE":
                raise ValueError(
                    "Live payouts refused: PAYOUT_MODE=live requires ENVIRONMENT=production and "
                    "LIVE_PAYOUTS_ACKNOWLEDGEMENT set to the documented phrase. Sandbox is the default."
                )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
