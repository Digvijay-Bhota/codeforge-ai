"""Application configuration via Pydantic Settings.

All settings are loaded from environment variables (or a .env file).
Never hard-code secrets — add them to .env.example as placeholders only.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_name: str = "CodeForge AI"
    app_env: str = "development"
    log_level: str = "INFO"

    # ── Database ───────────────────────────────────────────────────────────────
    # Full SQLAlchemy-compatible DSN.
    # SQLAlchemy / Alembic integration will be wired in Phase 1.
    database_url: str = "postgresql://codeforge:codeforge@postgres:5432/codeforge"

    # ── Redis ──────────────────────────────────────────────────────────────────
    # Task-queue / caching layer will be wired in a later phase.
    redis_url: str = "redis://redis:6379/0"


# Module-level singleton — import this from anywhere in the application.
settings = Settings()
