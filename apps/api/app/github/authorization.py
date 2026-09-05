"""Installation and execution authorization policies."""

import logging

from .webhook_models import GitHubWebhookEvent

logger = logging.getLogger(__name__)

class UnauthorizedInstallation(Exception):
    pass

class UnauthorizedRepository(Exception):
    pass

class GitHubInstallationAuthorizer:
    """Mockable authorizer for verifying GitHub App installations."""

    # In-memory store for Phase 6C.
    # Maps installation_id -> List of allowed full_names
    _allowed: dict[int, list[str]] = {
        # Expose a default test installation
        1: ["owner/repo"]
    }

    def authorize(self, event: GitHubWebhookEvent) -> bool:
        """Verify the installation and repository are authorized for CodeForge.

        For Phase 6C, this serves as a deterministic in-memory/config stub
        prior to database integration.
        """
        if not event.installation:
            return False

        install_id = event.installation.id
        if install_id not in self._allowed:
            return False

        allowed_repos = self._allowed[install_id]
        if event.repository.full_name not in allowed_repos:
            return False

        return True


class ExecutionAuthorizationPolicy:
    """Policy for determining if an event is allowed to execute write operations."""

    @staticmethod
    def is_authorized_for_write(event: GitHubWebhookEvent) -> bool:
        """Return True if the event explicitly authorizes agent execution.

        For Phase 6C, default to False. We only allow explicit commands in comments
        to trigger execution (which is mapped to a task intent).
        Issues opened and PRs opened are OBSERVATIONAL by default.
        """
        if event.event_type == "issue_comment" and event.action == "created":
            # Command parsing will extract intent; if it matches our explicit
            # command list, we treat it as write-authorized.
            return True

        # Issues/PRs do not automatically execute writes.
        return False
