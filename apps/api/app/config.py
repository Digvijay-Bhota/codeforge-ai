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
    database_url: str = "postgresql+asyncpg://codeforge:codeforge@postgres:5432/codeforge"

    # ── Redis ──────────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── Phase 7: Task Persistence & Async ──────────────────────────────────────
    task_queue_name: str = "codeforge:tasks"
    task_worker_id: str = "worker-1"
    task_lease_seconds: int = 300
    outbox_batch_size: int = 50
    outbox_poll_interval: float = 1.0
    task_output_max_bytes: int = 10000

    # ── Phase 1: LLM / Agent ───────────────────────────────────────────────────
    # Set OPENAI_API_KEY in .env (never commit the real key).
    openai_api_key: str = ""

    # OpenAI model used by the coding agent.
    codeforge_model: str = "gpt-4o"

    # Root directory under which workspaces are resolved.
    # Empty string means callers may supply absolute paths directly.
    workspace_root: str = ""


    # ── Phase 6: GitHub Integration ────────────────────────────────────────────
    # Set GITHUB_TOKEN in .env (never commit the real token).
    github_token: str = ""
    github_api_url: str = "https://api.github.com"

    # GitHub App configuration (Phase 6C)
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    github_app_name: str = "CodeForge"

# Module-level singleton — import this from anywhere in the application.
settings = Settings()
