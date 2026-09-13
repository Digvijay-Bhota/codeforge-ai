"""Application configuration via Pydantic Settings.

All settings are loaded from environment variables (or a .env file).
Never hard-code secrets — add them to .env.example as placeholders only.
"""

from pydantic import Field, model_validator
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
    app_env: str = Field(default="development", description="development, test, or production")
    log_level: str = "INFO"

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = Field(default="postgresql+asyncpg://codeforge:codeforge@postgres:5432/codeforge")
    db_pool_size: int = Field(default=20, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_recycle: int = Field(default=3600, ge=-1)

    # ── Redis ──────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://redis:6379/0")

    # ── Phase 7: Task Persistence & Async ──────────────────────────────────────
    task_queue_name: str = "codeforge:tasks"
    task_worker_id: str = "worker-1"
    task_lease_seconds: int = Field(default=300, ge=30)
    outbox_batch_size: int = Field(default=50, le=1000)
    outbox_poll_interval: float = Field(default=1.0, ge=0.1)
    task_output_max_bytes: int = 10000

    # Worker limits
    worker_concurrency: int = Field(default=5, ge=1, le=100)

    # ── Phase 1: LLM / Agent ───────────────────────────────────────────────────
    openai_api_key: str = ""
    codeforge_model: str = "gpt-4o"
    workspace_root: str = ""

    # ── Phase 6: GitHub Integration ────────────────────────────────────────────
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    github_app_name: str = "CodeForge"

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.app_env == "production":
            if not self.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required in production")
            # In production, we might require more strict checks
            if self.database_url == "postgresql+asyncpg://codeforge:codeforge@postgres:5432/codeforge":
                raise ValueError("DATABASE_URL must be explicitly configured in production")
        return self

# Module-level singleton
settings = Settings()
