"""Which optional capabilities this process is configured for, without secrets.

Reported as booleans and modes only, never values: logged by every process at
startup and served to signed-in users (GET /api/v1/system/status), so an
operator can see why, say, repository tasks are denied without reading .env.
Each process reports what it can know: the API does not receive the agent
model key, so only the worker reports that.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.config import DEFAULT_JWT_SECRET, Settings

logger = logging.getLogger(__name__)

# The value shipped in .env.example; set but unusable.
OPENAI_KEY_PLACEHOLDER = "sk-your-openai-api-key-here"  # nosec B105


def configuration_status(settings: Settings) -> dict[str, Any]:
    """Capabilities this process can use.  Safe to log and to show signed-in users."""
    jwt_secret = settings.jwt_secret_key
    return {
        "github_oauth": bool(settings.github_client_id and settings.github_client_secret),
        "github_app": settings.github_app_configured(),
        "github_webhooks": bool(settings.github_webhook_secret),
        "local_execution": bool(settings.codeforge_allow_local_execution and settings.workspace_root),
        "local_execution_trusted_accounts": len(settings.codeforge_local_execution_github_ids),
        "test_execution_mode": settings.codeforge_test_execution_mode.value,
        "session_secret": (
            "development-default"
            if jwt_secret == DEFAULT_JWT_SECRET or "insecure" in jwt_secret.lower()
            else "configured"
        ),
    }


def agent_model_key_status() -> str:
    """'configured', 'placeholder' or 'missing'.  The agent SDK reads OPENAI_API_KEY itself."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return "missing"
    return "placeholder" if key == OPENAI_KEY_PLACEHOLDER else "configured"


def log_configuration_status(settings: Settings, component: str, *, include_agent: bool = False) -> None:
    """One startup line per process; what is off, and therefore what will be refused."""
    status = configuration_status(settings)
    if include_agent:
        status["agent_model_key"] = agent_model_key_status()
    summary = " ".join(f"{key}={value}" for key, value in status.items())
    logger.info("%s configuration: %s", component, summary)
    if not status["github_app"]:
        logger.warning(
            "%s: GitHub App not configured (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY); repository "
            "permission checks deny access and GitHub tasks cannot run",
            component,
        )
