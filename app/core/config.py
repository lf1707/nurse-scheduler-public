"""Application configuration via environment variables."""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    APP_NAME: str = "Nurse Scheduler"
    APP_VERSION: str = "development"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    ENABLE_TEST_TENANTS: bool = False
    ENABLE_DEMO_TENANT: bool = False
    DEMO_TENANT_PASSWORD: str = "demo-password-change-me"
    DEMO_TENANT_GENERATE_SCHEDULE: bool = True

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://nurse:nurse_dev_pass@localhost:5432/nurse_scheduler"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 1800  # seconds

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CELERY_BEAT_SCHEDULE_FILENAME: str = "./ops-state/celerybeat-schedule"

    # Auth / JWT
    JWT_SECRET: str = "dev-jwt-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    # Refresh token TTL = the idle window. As long as the user is active
    # within this window, the frontend auto-refreshes the access token;
    # after this much idle time the refresh token itself expires → re-login.
    JWT_REFRESH_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES: int = 15
    LOGIN_FAILURE_WINDOW_SECONDS: int = 900
    LOGIN_MAX_FAILURES_PER_EMAIL_IP: int = 5
    LOGIN_MAX_FAILURES_PER_IP: int = 20
    TRUST_PROXY_FOR_CLIENT_IP: bool = False

    # Prospective-tenant applications
    PUBLIC_BASE_URL: str = ""
    TENANT_APPLICATION_VERIFICATION_HOURS: int = 24
    TENANT_APPLICATION_SETUP_HOURS: int = 72
    TENANT_APPLICATION_REJECTION_COOLDOWN_DAYS: int = 7
    TENANT_APPLICATION_MAX_PER_IP_PER_HOUR: int = 10
    TENANT_APPLICATION_MAX_PER_EMAIL_PER_DAY: int = 3
    TENANT_APPLICATION_MIN_SUBMIT_SECONDS: int = 2
    TENANT_APPLICATION_AUTO_APPROVE: bool = False
    TENANT_APPLICATION_AUTO_PLAN: str = "free"
    EMAIL_TRANSPORT: str = "log"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "Nurse Scheduler <no-reply@example.com>"
    SMTP_STARTTLS: bool = True

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    # Production Host allow-list. Empty derives the host from PUBLIC_BASE_URL.
    ALLOWED_HOSTS: list[str] = []

    # Solver defaults
    SOLVER_TIMEOUT_SECONDS: int = 120
    SOLVER_NUM_WORKERS: int = 8
    SOLVER_MAX_NURSE_DAYS: int = Field(default=100_000, ge=1)

    # Super admin (seeded on first migration)
    SUPER_ADMIN_EMAIL: str = "admin@example.com"
    SUPER_ADMIN_PASSWORD: str = "changeme123"

    # Logging
    LOG_LEVEL: str = "INFO"
    # Audit export
    BACKUP_PASSPHRASE: str = ""
    AUDIT_EXPORT_DIR: str = "./audit-exports"
    AUDIT_LOCK_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0)
    AUDIT_EXPORT_LOCK_RETRIES: int = Field(default=2, ge=0)
    AUDIT_LOCK_RETRY_DELAY_SECONDS: float = Field(default=0.25, ge=0)
    AUDIT_RETENTION_DAYS_HOT: int = 180  # searchable in PostgreSQL
    AUDIT_RETENTION_DAYS_COLD: int = 1095  # 3 years offline archive
    AUDIT_PARTITION_MONTHS_AHEAD: int = 3
    OPS_STATE_DIR: str = "./ops-state"
    OPS_CONTROL_DIR: str = "./ops-control"
    HOST_METRICS_PATH: str | None = None
    AUDIT_EXPORT_MAX_LAG_HOURS: int = 48
    # Anomaly alerting (security-hardening: audit anomaly detection).
    # Each rule scans recent audit events in a rolling window and raises
    # an alert when its threshold is crossed. Defaults are sane baselines;
    # production may tune them. Thresholds of 0 disable the rule.
    ALERT_LOGIN_FAILURE_RATE: int = 20
    ALERT_IMPERSONATION_RATE: int = 5
    ALERT_SUPER_ADMIN_ACTIONS: int = 100
    ALERT_SIGNUP_RATE: int = 10
    ALERT_EXPORT_RATE: int = 1000
    ANOMALY_ALERT_WINDOW_SECONDS: int = 3600
    ANOMALY_SCAN_SCHEDULE_SECONDS: int = Field(default=3600, ge=0)
    ALERT_EMAIL_RECIPIENT: str = ""
    ALERT_EMAIL_ENABLED: bool = True
    # Host filesystem watermark for periodic system health monitoring.
    # A threshold of 0 disables the check.
    DISK_USAGE_ALERT_PERCENT: int = 80
    DISK_USAGE_WARN_PERCENT: int = 70
    DISK_USAGE_CRITICAL_PERCENT: int = 90
    DISK_MIN_FREE_GB: float = 5.0
    DISK_ALERT_COOLDOWN_HOURS: int = 24
    ALERT_EMAIL_MAX_ATTEMPTS: int = 3
    ALERT_EMAIL_RETRY_SECONDS: float = 5.0
    DISK_USAGE_PATH: str = "/"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Accept JSON array string or list from env."""
        if isinstance(v, str):
            try:
                return cast(list[str], json.loads(v))
            except json.JSONDecodeError:
                # Comma-separated fallback
                return [origin.strip() for origin in v.split(",") if origin.strip()]
        return cast(list[str], v or [])

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                return cast(list[str], json.loads(v))
            except json.JSONDecodeError:
                return [host.strip() for host in v.split(",") if host.strip()]
        return cast(list[str], v or [])

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        if not self.is_prod:
            return self
        if self.LOGIN_FAILURE_WINDOW_SECONDS <= 0:
            raise ValueError("LOGIN_FAILURE_WINDOW_SECONDS must be positive")
        if self.LOGIN_MAX_FAILURES_PER_EMAIL_IP <= 0:
            raise ValueError("LOGIN_MAX_FAILURES_PER_EMAIL_IP must be positive")
        if self.LOGIN_MAX_FAILURES_PER_IP < self.LOGIN_MAX_FAILURES_PER_EMAIL_IP:
            raise ValueError("LOGIN_MAX_FAILURES_PER_IP must not be lower than the email limit")
        if self.EMAIL_TRANSPORT not in {"log", "smtp"}:
            raise ValueError("EMAIL_TRANSPORT must be 'log' or 'smtp'")
        if self.EMAIL_TRANSPORT == "smtp" and not self.SMTP_HOST:
            raise ValueError("SMTP_HOST is required when EMAIL_TRANSPORT=smtp")
        if not self.PUBLIC_BASE_URL.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use https:// in production")
        if self.EMAIL_TRANSPORT != "smtp":
            raise ValueError("EMAIL_TRANSPORT must be smtp in production")
        if self.DISK_USAGE_ALERT_PERCENT < 0 or self.DISK_USAGE_ALERT_PERCENT > 100:
            raise ValueError("DISK_USAGE_ALERT_PERCENT must be between 0 and 100")
        disk_percentages = (
            self.DISK_USAGE_WARN_PERCENT,
            self.DISK_USAGE_ALERT_PERCENT,
            self.DISK_USAGE_CRITICAL_PERCENT,
        )
        if any(value < 0 or value > 100 for value in disk_percentages):
            raise ValueError("DISK_USAGE_*_PERCENT values must be between 0 and 100")
        if self.DISK_USAGE_ALERT_PERCENT > 0 and not (
            0
            < self.DISK_USAGE_WARN_PERCENT
            < self.DISK_USAGE_ALERT_PERCENT
            < self.DISK_USAGE_CRITICAL_PERCENT
            <= 100
        ):
            raise ValueError(
                "DISK_USAGE_WARN_PERCENT, DISK_USAGE_ALERT_PERCENT, and "
                "DISK_USAGE_CRITICAL_PERCENT must be increasing and positive"
            )
        if self.DISK_MIN_FREE_GB < 0:
            raise ValueError("DISK_MIN_FREE_GB must not be negative")
        if self.DISK_ALERT_COOLDOWN_HOURS <= 0:
            raise ValueError("DISK_ALERT_COOLDOWN_HOURS must be positive")
        if self.AUDIT_EXPORT_MAX_LAG_HOURS <= 0:
            raise ValueError("AUDIT_EXPORT_MAX_LAG_HOURS must be positive")
        if self.ALERT_EMAIL_MAX_ATTEMPTS <= 0:
            raise ValueError("ALERT_EMAIL_MAX_ATTEMPTS must be positive")
        if self.ALERT_EMAIL_RETRY_SECONDS < 0:
            raise ValueError("ALERT_EMAIL_RETRY_SECONDS must not be negative")
        if any(
            value <= 0
            for value in (
                self.TENANT_APPLICATION_VERIFICATION_HOURS,
                self.TENANT_APPLICATION_SETUP_HOURS,
                self.TENANT_APPLICATION_REJECTION_COOLDOWN_DAYS,
                self.TENANT_APPLICATION_MAX_PER_IP_PER_HOUR,
                self.TENANT_APPLICATION_MAX_PER_EMAIL_PER_DAY,
                self.TENANT_APPLICATION_MIN_SUBMIT_SECONDS,
            )
        ):
            raise ValueError("TENANT_APPLICATION_* limits and durations must be positive")
        if not (
            0
            < self.IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES
            <= self.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        ):
            raise ValueError(
                "IMPERSONATION_ACCESS_TOKEN_EXPIRE_MINUTES must be positive and "
                "no longer than the access-token lifetime"
            )
        if not (
            0
            < self.IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES
            <= self.JWT_REFRESH_TOKEN_EXPIRE_MINUTES
        ):
            raise ValueError(
                "IMPERSONATION_REFRESH_TOKEN_EXPIRE_MINUTES must be positive and "
                "no longer than the refresh-token lifetime"
            )
        insecure_values = {
            "dev-secret-change-me",
            "dev-jwt-secret-change-me",
            "changeme123",
            "prod-smoke-test-secret-replace-in-real-deploy",
            "prod-smoke-test-jwt-replace-in-real-deploy",
            "demo-password-change-me",
        }
        if len(self.JWT_SECRET) < 32 or self.JWT_SECRET in insecure_values:
            raise ValueError("JWT_SECRET must be at least 32 characters in production")
        if self.SUPER_ADMIN_PASSWORD in insecure_values or len(self.SUPER_ADMIN_PASSWORD) < 12:
            raise ValueError("SUPER_ADMIN_PASSWORD must be strong in production")
        if self.ENABLE_DEMO_TENANT and (
            self.DEMO_TENANT_PASSWORD in insecure_values
            or self.DEMO_TENANT_PASSWORD.startswith("change-me")
            or len(self.DEMO_TENANT_PASSWORD) < 12
        ):
            raise ValueError("DEMO_TENANT_PASSWORD must be strong in production")
        if self.APP_DEBUG:
            raise ValueError("APP_DEBUG must be false in production")
        for host in self.ALLOWED_HOSTS:
            normalized_host = host.strip().lower()
            if (
                not normalized_host
                or normalized_host == "*"
                or any(char in normalized_host for char in "/?#@")
            ):
                raise ValueError(
                    "ALLOWED_HOSTS entries must be hostnames, not wildcards or URLs"
                )
        return self

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {"localhost"}
        hosts.update(host.strip().lower() for host in self.ALLOWED_HOSTS if host.strip())
        if self.PUBLIC_BASE_URL:
            public_host = urlsplit(self.PUBLIC_BASE_URL).hostname
            if public_host:
                hosts.add(public_host.lower())
        return hosts

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    s = Settings()
    # In dev mode, auto-derive a git-based version if APP_VERSION was not
    # explicitly set via env/build-arg. Keeps the footer accurate across
    # --reload restarts without manually restarting the container.
    if s.is_dev and s.APP_VERSION == "development":
        import pathlib
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=str(pathlib.Path(__file__).resolve().parent.parent.parent),
            )
            if result.returncode == 0 and result.stdout.strip():
                s.APP_VERSION = result.stdout.strip()
        except Exception:
            pass  # keep "development" if git is unavailable
    return s


settings = get_settings()
