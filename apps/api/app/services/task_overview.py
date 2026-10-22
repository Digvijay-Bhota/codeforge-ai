"""Read models for the console's overview and task activity.  Nothing here changes a task.

Scope follows app/api/task_access.py exactly, in SQL: a local task, or one
with no repository, is visible to its creator only; any other task is visible
to whoever can read its repository.  Repository permissions are resolved per
registered repository (the same cached GitHub check the task pages use), at
most MAX_SCOPE_REPOSITORIES of them; the response says when that bound was hit.

Everything returned is recorded data: task rows, status transitions
(task_events), approval requests and decisions (task_approvals), GitHub
origins (task_github_links) and retries (parent_task_id).  Nothing is inferred
or back-filled; history the system never recorded is reported as partial.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import (
    PUBLICATION_APPROVAL,
    GitHubIdentity,
    GitHubInstallation,
    GitHubInstallationRepository,
    Task,
    TaskApproval,
    TaskEvent,
    TaskGitHubLink,
    TaskStatusEnum,
    User,
)
from app.observability.redaction import redact_secrets
from app.schemas.task import (
    ActivityItem,
    ActivityPreview,
    AttentionItem,
    GitHubOrigin,
    TaskActivityResponse,
    TaskOverviewResponse,
)
from app.services.authorization_service import AuthorizationService

MAX_SCOPE_REPOSITORIES = 50
NEEDS_ATTENTION_LIMIT = 20
RECENT_ACTIVITY_LIMIT = 15
# Failed tasks count as needing attention for this long, unless retried.
FAILED_WINDOW_DAYS = 14
ACTIVITY_EVENT_LIMIT = 200
ACTIVITY_RETRY_LIMIT = 20
PREVIEW_TEXT_LIMIT = 2000

_DECISIONS = (TaskStatusEnum.WAITING_APPROVAL.value, TaskStatusEnum.WAITING_PUBLICATION.value)
_ATTENTION = {
    TaskStatusEnum.WAITING_APPROVAL.value: "plan_approval",
    TaskStatusEnum.WAITING_PUBLICATION.value: "publication_review",
    TaskStatusEnum.FAILED.value: "failed",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _creator_only(task: type[Task]) -> ColumnElement[bool]:
    """Tasks governed by their creator, as task_access._governing_repository decides."""
    return or_(task.execution_target == "local", task.repository.is_(None), task.repository == "")


async def readable_repositories(session: AsyncSession, user: User) -> tuple[list[str], bool]:
    """Registered repositories *user* may read, and whether the bound cut the list short."""
    names = (
        (
            await session.execute(
                select(GitHubInstallationRepository.repository)
                .join(
                    GitHubInstallation,
                    GitHubInstallation.installation_id
                    == GitHubInstallationRepository.installation_id,
                )
                .where(
                    GitHubInstallationRepository.authorized.is_(True),
                    GitHubInstallation.active.is_(True),
                )
                .distinct()
                .order_by(GitHubInstallationRepository.repository)
                .limit(MAX_SCOPE_REPOSITORIES + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(names) > MAX_SCOPE_REPOSITORIES
    auth = AuthorizationService(session)
    readable = [
        name
        for name in names[:MAX_SCOPE_REPOSITORIES]
        if await auth.can_read_repository(user, name)
    ]
    return readable, truncated


def visible_to(user: User, repositories: list[str]) -> ColumnElement[bool]:
    creator = and_(_creator_only(Task), Task.creator_id == user.id)
    if not repositories:
        return creator
    return or_(creator, and_(~_creator_only(Task), Task.repository.in_(repositories)))


async def _origins(session: AsyncSession, task_ids: list[str]) -> dict[str, GitHubOrigin]:
    if not task_ids:
        return {}
    links = (
        await session.execute(select(TaskGitHubLink).where(TaskGitHubLink.task_id.in_(task_ids)))
    ).scalars()
    return {
        link.task_id: GitHubOrigin(
            issue_number=None if link.pull_request_number else link.issue_number,
            pull_request_number=link.pull_request_number,
            triggered_by=link.triggering_github_login,
        )
        for link in links
    }


async def task_overview(session: AsyncSession, user: User) -> TaskOverviewResponse:
    repositories, truncated = await readable_repositories(session, user)
    scope = visible_to(user, repositories)

    counts = {
        status: int(n)
        for status, n in (
            await session.execute(
                select(Task.status, func.count()).where(scope).group_by(Task.status)
            )
        ).all()
    }

    child = aliased(Task)
    since = datetime.now(UTC) - timedelta(days=FAILED_WINDOW_DAYS)
    attention = and_(
        scope,
        or_(
            Task.status.in_(_DECISIONS),
            and_(
                Task.status == TaskStatusEnum.FAILED.value,
                Task.updated_at >= since,
                ~exists().where(child.parent_task_id == Task.task_id),
            ),
        ),
    )
    attention_total = int(
        (
            await session.execute(select(func.count()).select_from(Task).where(attention))
        ).scalar_one()
    )
    attention_tasks = (
        (
            await session.execute(
                select(Task)
                .where(attention)
                # Decisions first (someone is waiting on them), then the latest failures.
                .order_by(
                    (Task.status == TaskStatusEnum.FAILED.value).asc(), Task.updated_at.desc()
                )
                .limit(NEEDS_ATTENTION_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    origins = await _origins(session, [t.task_id for t in attention_tasks])

    recent = (
        await session.execute(
            select(TaskEvent, Task.repository, Task.requested_task)
            .join(Task, Task.task_id == TaskEvent.task_id)
            .where(scope)
            .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
            .limit(RECENT_ACTIVITY_LIMIT)
        )
    ).all()

    return TaskOverviewResponse(
        total=sum(counts.values()),
        counts=counts,
        needs_attention_total=attention_total,
        needs_attention=[
            AttentionItem(
                task_id=t.task_id,
                status=t.status,
                execution_target=t.execution_target,
                repository=t.repository,
                requested_task=t.requested_task,
                created_at=_iso(t.created_at),
                started_at=_iso(t.started_at),
                completed_at=_iso(t.completed_at),
                parent_task_id=t.parent_task_id,
                has_plan=bool(t.implementation_plan),
                has_diff=bool(isinstance(t.task_result, dict) and t.task_result.get("diff")),
                failure_code=t.failure_code,
                github=origins.get(t.task_id),
                attention=_ATTENTION[t.status],
                updated_at=_iso(t.updated_at),
            )
            for t in attention_tasks
        ],
        failed_window_days=FAILED_WINDOW_DAYS,
        recent_activity=[
            ActivityPreview(
                task_id=event.task_id,
                repository=repository,
                requested_task=requested[:PREVIEW_TEXT_LIMIT],
                from_status=event.from_status,
                to_status=event.to_status,
                at=event.created_at.isoformat(),
            )
            for event, repository, requested in recent
        ],
        repositories=repositories,
        repositories_truncated=truncated,
        generated_at=datetime.now(UTC).isoformat(),
    )


def _approval_kind(approval: TaskApproval) -> str:
    return "publication" if approval.approval_type == PUBLICATION_APPROVAL else "plan"


async def task_activity(session: AsyncSession, task: Task, viewer: User) -> TaskActivityResponse:
    """The recorded history of *task*, oldest first.  The caller has checked read access."""
    dated: list[tuple[datetime, ActivityItem]] = []

    link = (
        await session.execute(select(TaskGitHubLink).where(TaskGitHubLink.task_id == task.task_id))
    ).scalar_one_or_none()
    creator_login = None
    if task.creator_id:
        creator_login = (
            await session.execute(
                select(GitHubIdentity.github_login).where(GitHubIdentity.user_id == task.creator_id)
            )
        ).scalar_one_or_none()
    dated.append(
        (
            task.created_at,
            ActivityItem(
                kind="created",
                at=task.created_at.isoformat(),
                to_status=TaskStatusEnum.PENDING.value,
                actor=link.triggering_github_login if link else creator_login,
                related_task_id=task.parent_task_id,
                github=(
                    GitHubOrigin(
                        issue_number=None if link.pull_request_number else link.issue_number,
                        pull_request_number=link.pull_request_number,
                        triggered_by=link.triggering_github_login,
                    )
                    if link
                    else None
                ),
            ),
        )
    )

    total_events = int(
        (
            await session.execute(
                select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == task.task_id)
            )
        ).scalar_one()
    )
    events = (
        (
            await session.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.task_id)
                .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
                .limit(ACTIVITY_EVENT_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    for event in events:
        dated.append(
            (
                event.created_at,
                ActivityItem(
                    kind="status",
                    at=event.created_at.isoformat(),
                    from_status=event.from_status,
                    to_status=event.to_status,
                    message=redact_secrets(event.message) if event.message else None,
                ),
            )
        )

    approvals = (
        (
            await session.execute(
                select(TaskApproval)
                .where(TaskApproval.task_id == task.task_id)
                .order_by(TaskApproval.id.asc())
            )
        )
        .scalars()
        .all()
    )
    for approval in approvals:
        kind = _approval_kind(approval)
        dated.append(
            (
                approval.requested_at,
                ActivityItem(
                    kind="approval_requested",
                    at=approval.requested_at.isoformat(),
                    approval_id=approval.id,
                    approval_type=kind,
                    approval_status="PENDING",
                ),
            )
        )
        if approval.responded_at is not None and approval.status != "PENDING":
            dated.append(
                (
                    approval.responded_at,
                    ActivityItem(
                        kind="approval_decided",
                        at=approval.responded_at.isoformat(),
                        approval_id=approval.id,
                        approval_type=kind,
                        approval_status=approval.status,
                        # Expiry is decided by the clock, not a person.
                        actor=approval.approved_by if approval.status != "EXPIRED" else None,
                        message=redact_secrets(approval.comment) if approval.comment else None,
                    ),
                )
            )

    # Retries of this task the viewer may read: same repository and target, and
    # for creator-only tasks the viewer's own.
    retry_scope = and_(
        Task.parent_task_id == task.task_id,
        Task.execution_target == task.execution_target,
        Task.repository.is_(None)
        if task.repository is None
        else Task.repository == task.repository,
    )
    if task.execution_target == "local" or not task.repository:
        retry_scope = and_(retry_scope, Task.creator_id == viewer.id)
    retries = (
        await session.execute(
            select(Task.task_id, Task.created_at)
            .where(retry_scope)
            .order_by(Task.created_at.asc())
            .limit(ACTIVITY_RETRY_LIMIT)
        )
    ).all()
    for retry_id, created in retries:
        dated.append(
            (
                created,
                ActivityItem(kind="retried", at=created.isoformat(), related_task_id=retry_id),
            )
        )

    # Chronological; facts recorded at the same instant keep the order they were added in.
    items = [item for _, item in sorted(dated, key=lambda pair: pair[0])]

    partial_reason = None
    if not events and task.status != TaskStatusEnum.PENDING.value:
        partial_reason = (
            "No status changes were recorded for this task; it predates activity history."
        )
    elif events and events[0].from_status not in (None, TaskStatusEnum.PENDING.value):
        partial_reason = (
            f"Status changes before {events[0].created_at.isoformat()} were not recorded "
            f"(the first recorded change starts from {events[0].from_status})."
        )
    return TaskActivityResponse(
        task_id=task.task_id,
        items=items,
        partial=partial_reason is not None,
        partial_reason=partial_reason,
        truncated=total_events > len(events),
    )
