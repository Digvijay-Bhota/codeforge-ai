"""Read models for the console's overview and task activity.  Nothing here changes a task.

Scope follows app/api/task_access.py exactly, in SQL: a local task, or one
with no repository, is visible to its creator only; any other task is visible
to whoever can read its repository.  Repository permissions are resolved per
registered repository (the same cached GitHub check the task pages use), at
most MAX_SCOPE_REPOSITORIES of them; the response says when that bound was hit,
and when GitHub could not confirm some repository either way (left out).  It
never names those: one may be a private repository the viewer cannot read.

Everything returned is recorded data: task rows, status transitions
(task_events), approval requests and decisions (task_approvals), GitHub
origins (task_github_links) and retries (parent_task_id).  Nothing is inferred
or back-filled; history the system never recorded is reported as partial.
"""

from __future__ import annotations

from collections.abc import Sequence
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
from app.db.repositories.task_repository import matches_text
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
from app.services.queue_service import get_redis_client

MAX_SCOPE_REPOSITORIES = 50
# After this many repositories GitHub could not answer for, the overview stops
# asking it and answers the rest from the permission cache.  Unknown answers
# are never cached, so without a limit every overview request (the page polls)
# would ask GitHub about every repository throughout an outage.
MAX_UNKNOWN_PERMISSIONS = 2
# After GitHub fails to answer a permission check for an installation, the
# overview (and the "all I can read" list) do not ask it about that
# installation's repositories again for this long; they count them as
# unconfirmed meanwhile.  Shared by every user and tab through Redis: the
# pages poll every 5 s, and each unanswered ask can take ~40 s (client
# retries and timeouts).  Access decisions never use this pause.
GITHUB_PAUSE_SECONDS = 30


def github_pause_key(installation_id: int) -> str:
    return f"cf_overview_github_paused:{installation_id}"
NEEDS_ATTENTION_LIMIT = 20
RECENT_ACTIVITY_LIMIT = 15
# Failed tasks count as needing attention for this long, unless retried.
FAILED_WINDOW_DAYS = 14
ACTIVITY_EVENT_LIMIT = 200
ACTIVITY_RETRY_LIMIT = 20
PREVIEW_TEXT_LIMIT = 2000

_DECISIONS = (TaskStatusEnum.WAITING_APPROVAL.value, TaskStatusEnum.WAITING_PUBLICATION.value)
# The transition each decision makes, in the same transaction (task_service.py,
# api/tasks.py): (approval kind, decision) -> (from status, to status).
_DECISION_TRANSITION = {
    ("plan", "APPROVED"): (TaskStatusEnum.WAITING_APPROVAL.value, TaskStatusEnum.QUEUED.value),
    ("plan", "REJECTED"): (TaskStatusEnum.WAITING_APPROVAL.value, TaskStatusEnum.CANCELLED.value),
    ("plan", "EXPIRED"): (TaskStatusEnum.WAITING_APPROVAL.value, TaskStatusEnum.CANCELLED.value),
    ("publication", "APPROVED"): (
        TaskStatusEnum.WAITING_PUBLICATION.value,
        TaskStatusEnum.PUBLISHING.value,
    ),
    ("publication", "REJECTED"): (
        TaskStatusEnum.WAITING_PUBLICATION.value,
        TaskStatusEnum.CANCELLED.value,
    ),
    ("publication", "EXPIRED"): (
        TaskStatusEnum.WAITING_PUBLICATION.value,
        TaskStatusEnum.CANCELLED.value,
    ),
}
# A decision and its transition share a transaction; their timestamps differ
# by at most the transaction's length.  Anything further apart is unrelated.
_SAME_TRANSACTION = timedelta(seconds=60)
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


async def readable_repositories(session: AsyncSession, user: User) -> tuple[list[str], int, bool]:
    """Registered repositories *user* may read; how many GitHub could not
    confirm either way (left out, as access checks fail closed); and whether
    the bound cut the list short.  Only the readable ones are named.

    Cached answers are read in one round trip; only the rest are checked one
    by one, and GitHub is asked at most MAX_UNKNOWN_PERMISSIONS times, never
    for an installation it recently failed to answer for.
    """
    rows = (
        await session.execute(
            select(GitHubInstallationRepository.repository, func.min(GitHubInstallationRepository.installation_id))
            .join(
                GitHubInstallation,
                GitHubInstallation.installation_id == GitHubInstallationRepository.installation_id,
            )
            .where(
                GitHubInstallationRepository.authorized.is_(True),
                GitHubInstallation.active.is_(True),
            )
            .group_by(GitHubInstallationRepository.repository)
            .order_by(GitHubInstallationRepository.repository)
            .limit(MAX_SCOPE_REPOSITORIES + 1)
        )
    ).all()
    truncated = len(rows) > MAX_SCOPE_REPOSITORIES
    rows = rows[:MAX_SCOPE_REPOSITORIES]
    auth = AuthorizationService(session)
    cached = await auth.cached_repository_permissions(user, [name for name, _ in rows])
    paused = await _paused_installations({installation for name, installation in rows if name not in cached})

    readable: list[str] = []
    unconfirmed = 0
    for name, installation in rows:
        permission = cached.get(name)
        if permission is None:
            ask = unconfirmed < MAX_UNKNOWN_PERMISSIONS and installation not in paused
            permission = await auth.repository_permission_if_known(user, name, ask_github=ask)
            if permission is None and ask:
                paused.add(installation)
                await _pause_github(installation)
        if permission is None:
            unconfirmed += 1
        elif permission in ("read", "write", "admin"):
            readable.append(name)
    return readable, unconfirmed, truncated


async def _paused_installations(installations: set[int]) -> set[int]:
    if not installations:
        return set()
    ordered = sorted(installations)
    try:
        values = await get_redis_client().mget([github_pause_key(i) for i in ordered])
    except Exception:
        return set()  # no pause known: GitHub may be asked, within the per-request limit
    return {i for i, value in zip(ordered, values, strict=True) if value}


async def _pause_github(installation: int) -> None:
    try:
        await get_redis_client().setex(github_pause_key(installation), GITHUB_PAUSE_SECONDS, "1")
    except Exception:
        pass  # nosec B110 - best effort; the per-request limit still applies


def visible_to(user: User, repositories: list[str]) -> ColumnElement[bool]:
    creator = and_(_creator_only(Task), Task.creator_id == user.id)
    if not repositories:
        return creator
    return or_(creator, and_(~_creator_only(Task), Task.repository.in_(repositories)))


async def list_readable_tasks(
    session: AsyncSession,
    user: User,
    statuses: list[str] | None,
    limit: int,
    offset: int,
    search: str | None = None,
) -> tuple[list[Task], int, bool, bool]:
    """A page of every task *user* may read, newest first, in the overview's scope.

    Returns the tasks, the exact total, and whether repositories were left
    out (the bound, or GitHub could not confirm them) so tasks may be missing.
    """
    repositories, unconfirmed, truncated = await readable_repositories(session, user)
    where = visible_to(user, repositories)
    if statuses:
        where = and_(where, Task.status.in_(statuses))
    if search:
        where = and_(where, matches_text(search))
    total = int(
        (await session.execute(select(func.count()).select_from(Task).where(where))).scalar_one()
    )
    tasks = (
        (
            await session.execute(
                select(Task)
                .where(where)
                .order_by(Task.created_at.desc(), Task.task_id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(tasks), total, truncated, unconfirmed > 0


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
    repositories, unconfirmed, truncated = await readable_repositories(session, user)
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
    # When the task failed (completed_at, set by the state machine), not when
    # its row was last written; older rows without completed_at fall back.
    failed_at = func.coalesce(Task.completed_at, Task.updated_at)
    attention = and_(
        scope,
        or_(
            Task.status.in_(_DECISIONS),
            and_(
                Task.status == TaskStatusEnum.FAILED.value,
                failed_at >= since,
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
                # A waiting task has no completed_at, so it sorts by updated_at.
                .order_by((Task.status == TaskStatusEnum.FAILED.value).asc(), failed_at.desc())
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
        repositories_unconfirmed=unconfirmed > 0,
        generated_at=datetime.now(UTC).isoformat(),
    )


def _approval_kind(approval: TaskApproval) -> str:
    return "publication" if approval.approval_type == PUBLICATION_APPROVAL else "plan"


def _caused_transition(
    events: Sequence[TaskEvent], taken: set[int], approval: TaskApproval, kind: str
) -> int | None:
    """Index of the status change *approval*'s decision made, if it was recorded.

    The first unmatched transition out of the waiting status into the
    decision's outcome after the request, recorded within one transaction of
    the decision.  None when there is no such change, for example an expiry
    recorded after the task had already moved on.
    """
    transition = _DECISION_TRANSITION.get((kind, approval.status))
    if transition is None or approval.responded_at is None:
        return None
    for index, event in enumerate(events):
        if index in taken or (event.from_status, event.to_status) != transition:
            continue
        if event.created_at < approval.requested_at:
            continue
        if abs(approval.responded_at - event.created_at) <= _SAME_TRANSACTION:
            return index
        return None  # the first such change is unrelated to this decision
    return None


async def task_activity(session: AsyncSession, task: Task, viewer: User) -> TaskActivityResponse:
    """The recorded history of *task*, oldest first.  The caller has checked read access.

    Items are ordered by when they were recorded, then by *rank* for facts
    recorded at the same instant (one transaction stamps everything with the
    same now()): creation, then status changes in their recorded order, then
    approval requests, then retries.  A decision is placed just before the
    status change it caused: both are written together, but the approve and
    reject paths stamp them identically and the expiry paths stamp the
    decision a moment later, which would show the effect before its cause.
    The decision keeps its own recorded time.
    """
    # (recorded at, rank among facts recorded at that instant, item)
    dated: list[tuple[datetime, float, ActivityItem]] = []

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
            0,
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
    first_event_rank = 1
    for index, event in enumerate(events):
        dated.append(
            (
                event.created_at,
                first_event_rank + index,
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
    later_rank = first_event_rank + len(events)
    caused: set[int] = set()  # events already matched to a decision
    for approval in approvals:
        kind = _approval_kind(approval)
        dated.append(
            (
                approval.requested_at,
                later_rank,
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
            at, rank = approval.responded_at, float(later_rank)
            effect = _caused_transition(events, caused, approval, kind)
            if effect is not None:
                caused.add(effect)
                at, rank = events[effect].created_at, first_event_rank + effect - 0.5
            dated.append(
                (
                    at,
                    rank,
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
                later_rank + 1,
                ActivityItem(kind="retried", at=created.isoformat(), related_task_id=retry_id),
            )
        )

    # Stable: approvals at the same instant and rank keep their id order.
    items = [item for _, _, item in sorted(dated, key=lambda entry: (entry[0], entry[1]))]

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
