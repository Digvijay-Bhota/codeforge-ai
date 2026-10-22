"""Inspect the outbox and requeue failed events.  Runs with the service's own database credentials.

    python -m app.outbox_admin status            # queue health (read-only)
    python -m app.outbox_admin list --failed     # given-up events (read-only)
    python -m app.outbox_admin list --scheduled  # events waiting for a retry (read-only)
    python -m app.outbox_admin requeue 12 15 --by alice --reason "GitHub App reinstalled"

Nothing here prints an event's payload, a repository name or a credential:
events are shown by id, type, the id of what they concern (a task, a job or
a webhook delivery) and their delivery state, with the last error as stored
(already redacted and bounded).  Nothing is ever deleted.  See
docs/operations/outbox.md.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutboxEvent, TaskGitHubLink
from app.db.repositories.outbox_repository import OutboxRepository
from app.observability.events import AuditEventType
from app.observability.tracing import record_audit_event

LIST_LIMIT_MAX = 200
ERROR_PREVIEW = 200
STATUS_EVENT_TYPES = ("GITHUB_STATUS_UPDATE", "GITHUB_COMMAND_ACCEPTED", "JOB_STARTED", "JOB_COMPLETED", "JOB_FAILED")

_PENDING = and_(OutboxEvent.published_at.is_(None), OutboxEvent.failed_at.is_(None))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def queue_status(session: AsyncSession) -> dict[str, Any]:
    """Counts and ages of the queue: aggregate queries only, no rows."""
    now = (await session.execute(select(func.now()))).scalar_one()
    due = and_(_PENDING, or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now))
    scheduled = and_(_PENDING, OutboxEvent.next_attempt_at > now)

    async def count(where: Any) -> int:
        return int((await session.execute(select(func.count()).select_from(OutboxEvent).where(where))).scalar_one())

    oldest_pending = (await session.execute(select(func.min(OutboxEvent.created_at)).where(_PENDING))).scalar_one()
    next_retry = (await session.execute(select(func.min(OutboxEvent.next_attempt_at)).where(scheduled))).scalar_one()
    failed_by = (
        await session.execute(
            select(OutboxEvent.event_type, OutboxEvent.last_error_category, func.count())
            .where(OutboxEvent.failed_at.is_not(None))
            .group_by(OutboxEvent.event_type, OutboxEvent.last_error_category)
            .order_by(OutboxEvent.event_type)
        )
    ).all()
    scheduled_by = (
        await session.execute(
            select(OutboxEvent.event_type, OutboxEvent.last_error_category, func.count())
            .where(scheduled)
            .group_by(OutboxEvent.event_type, OutboxEvent.last_error_category)
            .order_by(OutboxEvent.event_type)
        )
    ).all()
    return {
        "checked_at": now.isoformat(),
        "due_now": await count(due),
        "scheduled_for_retry": await count(scheduled),
        "failed": await count(OutboxEvent.failed_at.is_not(None)),
        "oldest_pending_created_at": _iso(oldest_pending),
        "oldest_pending_age_seconds": int((now - oldest_pending).total_seconds()) if oldest_pending else None,
        "next_retry_at": _iso(next_retry),
        "failed_by_type": [{"event_type": t, "category": c, "count": n} for t, c, n in failed_by],
        "scheduled_by_type": [{"event_type": t, "category": c, "count": n} for t, c, n in scheduled_by],
    }


async def list_events(session: AsyncSession, *, failed: bool, limit: int = 50) -> list[dict[str, Any]]:
    """Failed (most recent first) or scheduled (soonest first) events, without payloads, bounded."""
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    where: ColumnElement[bool]
    if failed:
        where = OutboxEvent.failed_at.is_not(None)
        order = (OutboxEvent.failed_at.desc(), OutboxEvent.id.desc())
    else:
        where = and_(_PENDING, OutboxEvent.next_attempt_at.is_not(None))
        order = (OutboxEvent.next_attempt_at.asc(), OutboxEvent.id.asc())
    events = (await session.execute(select(OutboxEvent).where(where).order_by(*order).limit(limit))).scalars().all()

    # For status events: whether the task's check run exists already (it is
    # adopted, not duplicated, when the event is retried).
    task_ids = [e.aggregate_id for e in events if e.event_type in STATUS_EVENT_TYPES]
    check_runs = {}
    if task_ids:
        rows = await session.execute(
            select(TaskGitHubLink.task_id, TaskGitHubLink.check_run_id).where(TaskGitHubLink.task_id.in_(task_ids))
        )
        check_runs = {task_id: run_id for task_id, run_id in rows.all()}
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "concerns": e.aggregate_id,
            "created_at": _iso(e.created_at),
            "attempts": e.attempts,
            "category": e.last_error_category,
            "first_failed_at": _iso(e.first_failed_at),
            "last_failed_at": _iso(e.last_failed_at),
            "next_attempt_at": _iso(e.next_attempt_at),
            "failed_at": _iso(e.failed_at),
            "last_error": (e.last_error or "")[:ERROR_PREVIEW] or None,
            "check_run_recorded": (check_runs.get(e.aggregate_id) is not None)
            if e.event_type in STATUS_EVENT_TYPES
            else None,
        }
        for e in events
    ]


async def requeue(session: AsyncSession, event_ids: list[int], *, by: str, reason: str) -> dict[int, bool]:
    """Requeue each failed event (one transaction); False for an id that is not a failed event."""
    repo = OutboxRepository(session)
    results: dict[int, bool] = {}
    for event_id in event_ids:
        results[event_id] = await repo.requeue(event_id)
        if results[event_id]:
            await record_audit_event(
                session,
                event_type=AuditEventType.OUTBOX_EVENT_REQUEUED,
                actor_type="OPERATOR",
                actor_id=by,
                resource_type="outbox_event",
                resource_id=str(event_id),
                result="REQUEUED",
                metadata={"reason": reason},
            )
    await session.commit()
    return results


def _print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c) if r.get(c) is not None else "-")) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c) if row.get(c) is not None else "-").ljust(widths[c]) for c in columns))


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.outbox_admin", description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="queue health (read-only)")
    listing = sub.add_parser("list", help="failed or scheduled events (read-only)")
    which = listing.add_mutually_exclusive_group(required=True)
    which.add_argument("--failed", action="store_true")
    which.add_argument("--scheduled", action="store_true")
    listing.add_argument("--limit", type=int, default=50, help=f"at most {LIST_LIMIT_MAX}")
    again = sub.add_parser("requeue", help="make failed events due again, with a fresh retry budget")
    again.add_argument("ids", nargs="+", type=int)
    again.add_argument("--by", default=None, help="who is requeueing (default: the OS user)")
    again.add_argument("--reason", required=True, help="why it can succeed now (recorded in the audit log)")
    args = parser.parse_args(argv)

    from app.db.session import async_session_maker, engine

    try:
        async with async_session_maker() as session:
            if args.command == "status":
                status = await queue_status(session)
                if args.json:
                    print(json.dumps(status, indent=2))
                else:
                    for key in ("checked_at", "due_now", "scheduled_for_retry", "failed",
                                "oldest_pending_created_at", "oldest_pending_age_seconds", "next_retry_at"):  # fmt: skip
                        print(f"{key}: {status[key] if status[key] is not None else '-'}")
                    for title, key in (("failed", "failed_by_type"), ("scheduled", "scheduled_by_type")):
                        if status[key]:
                            print(f"\n{title} by type and category:")
                            _print_table(status[key], ["event_type", "category", "count"])
                return 0
            if args.command == "list":
                rows = await list_events(session, failed=args.failed, limit=args.limit)
                if args.json:
                    print(json.dumps(rows, indent=2))
                else:
                    columns = ["id", "event_type", "concerns", "attempts", "category",
                               "failed_at" if args.failed else "next_attempt_at", "check_run_recorded", "last_error"]  # fmt: skip
                    _print_table(rows, columns)
                return 0
            results = await requeue(session, args.ids, by=args.by or getpass.getuser(), reason=args.reason)
            for event_id, done in results.items():
                print(f"{event_id}: {'requeued' if done else 'not a failed event; unchanged'}")
            return 0 if all(results.values()) else 1
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
