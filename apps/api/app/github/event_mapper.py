"""Maps typed webhook events into CodeForge task requests."""

import logging

from app.schemas.task import ExecutionTarget, TaskRequest

from .authorization import ExecutionAuthorizationPolicy
from .command_parser import CommandParser, ParsedCommand
from .webhook_models import GitHubWebhookEvent

logger = logging.getLogger(__name__)

class EventMapper:
    """Maps GitHub webhook events to TaskRequest intents."""

    @staticmethod
    def map_event(event: GitHubWebhookEvent) -> TaskRequest | None:
        """Convert a webhook event to a CodeForge task request.

        Returns None if the event should not trigger a task.
        """
        # 1. Evaluate Execution Policy
        if not ExecutionAuthorizationPolicy.is_authorized_for_write(event):
            # For Phase 6C, we only process explicitly authorized write events (commands).
            # We will ignore issues.opened and pull_requests without explicit commands
            # for now, as we lack a safe observational review mode.
            logger.info("Event %s.%s is not authorized for task execution. Ignoring.", event.event_type, event.action)
            return None

        # 2. Map Issue Comments
        if event.event_type == "issue_comment" and event.action == "created" and event.comment and event.issue:
            parsed_cmd = CommandParser.parse(event.comment.body)
            if not parsed_cmd:
                return None

            return EventMapper._build_task_request(
                event=event,
                description=EventMapper._format_issue_command_description(parsed_cmd, event)
            )

        return None

    @staticmethod
    def _build_task_request(event: GitHubWebhookEvent, description: str) -> TaskRequest:
        """Construct the core TaskRequest for GitHub execution."""
        return TaskRequest(
            execution_target=ExecutionTarget.github,
            github_repository=event.repository.full_name,
            github_base_branch=event.repository.default_branch,
            description=description[:10000] # Bound the prompt length
        )

    @staticmethod
    def _format_issue_command_description(cmd: ParsedCommand, event: GitHubWebhookEvent) -> str:
        """Format the task description based on the command and issue context."""
        desc = f"User explicitly requested to `{cmd.command.value}` this issue.\n"
        if cmd.arguments:
            desc += f"\nCommand arguments / user instructions:\n{cmd.arguments}\n"

        if event.issue:
            desc += f"\nIssue Title: {event.issue.title}\n"
            if event.issue.body:
                desc += f"\nIssue Body:\n{event.issue.body}\n"

        return desc
