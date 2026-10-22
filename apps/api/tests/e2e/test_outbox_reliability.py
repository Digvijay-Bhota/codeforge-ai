"""Outbox delivery through GitHub outages, lost responses and crashes, without duplicates.

The real webhook, outbox dispatcher, status consumer and GitHub client run
against the fake GitHub of test_comment_to_approval.py over HTTP.  Faults
model an outage (GitHub refuses), and a lost response (GitHub did the work,
then the response failed).  "Time passing" makes every scheduled retry due
now instead of sleeping.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text

from app.db.models import OutboxEvent, TaskGitHubLink
from tests.e2e.fake_github import Fault
from tests.e2e.test_comment_to_approval import (  # noqa: F401
    COMMAND,
    deliver,
    dispatch_all,
    e2e,
    login,
)
from tests.e2e.test_github_failures import no_backoff  # noqa: F401

ACK_MARKER = "codeforge:managed-comment"


async def let_time_pass(env: dict[str, Any]) -> None:
    """Every retry scheduled for later becomes due now, as if its delay had elapsed."""
    async with env["maker"]() as s:
        await s.execute(text("UPDATE outbox_events SET next_attempt_at = now() WHERE next_attempt_at > now()"))
        await s.commit()


async def pr_comment(env: dict[str, Any]) -> None:
    """A writer's @codeforge comment on a pull request whose head is the fake repository's head commit."""
    world = env["world"]
    raw = world.payload(login(world, "dev"), COMMAND, pull_request_head=env["head"])
    assert (await deliver(env, raw)).status_code == 202


def acks(env: dict[str, Any]) -> list[str]:
    world = env["world"]
    return [b for b in env["fake"].comment_bodies(world.full_name, 2) if ACK_MARKER in b]


async def status_events(env: dict[str, Any]) -> list[OutboxEvent]:
    async with env["maker"]() as s:
        rows = await s.execute(select(OutboxEvent).where(OutboxEvent.event_type == "GITHUB_STATUS_UPDATE"))
        return list(rows.scalars())


@pytest.mark.asyncio
async def test_an_outage_longer_than_the_retry_budget_does_not_lose_the_status(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    fake, repo = e2e["fake"], e2e["world"].full_name
    outage = [Fault(m, rf"/repos/{repo}/(issues/2/comments|check-runs)", 502) for m in ("GET", "POST")]
    fake.faults += outage
    await pr_comment(e2e)
    for _ in range(6):  # the dispatcher keeps polling through a long outage
        await dispatch_all(e2e)
        await let_time_pass(e2e)
    assert acks(e2e) == [] and fake.runs(repo) == []

    for fault in outage:  # GitHub recovers
        fake.faults.remove(fault)
    await let_time_pass(e2e)
    await dispatch_all(e2e)

    assert len(acks(e2e)) == 1, "the acknowledgement was never posted"
    assert [r["status"] for r in fake.runs(repo)] == ["queued"], "the check run was never created"


@pytest.mark.asyncio
async def test_a_lost_create_response_does_not_create_a_second_check_run(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    # Case A: GitHub creates the check run, but CodeForge never sees the response.
    fake, repo = e2e["fake"], e2e["world"].full_name
    fake.faults.append(Fault("POST", rf"/repos/{repo}/check-runs", 502, times=1, after=True))
    await pr_comment(e2e)
    await dispatch_all(e2e)
    await let_time_pass(e2e)
    await dispatch_all(e2e)

    runs = fake.runs(repo)
    assert len(runs) == 1, f"{len(runs)} check runs for one task"
    async with e2e["maker"]() as s:
        link = (await s.execute(select(TaskGitHubLink))).scalar_one()
    assert link.check_run_id == runs[0]["id"]  # adopted, not orphaned


@pytest.mark.asyncio
async def test_a_crash_after_creating_the_check_run_does_not_create_a_second(
    e2e: dict[str, Any], no_backoff: None, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    # Case A, locally: the check run exists on GitHub, then saving its ID fails.
    from sqlalchemy.exc import OperationalError

    import app.github.status_consumer as status_consumer

    real = status_consumer.record_audit_event
    failed = []

    async def failing_once(session: Any, event_type: Any, **kwargs: Any) -> Any:
        if str(event_type).endswith("CHECK_RUN_CREATED") and not failed:
            failed.append(event_type)
            raise OperationalError("INSERT INTO audit_events", {}, Exception("server closed the connection"))
        return await real(session, event_type=event_type, **kwargs)

    monkeypatch.setattr(status_consumer, "record_audit_event", failing_once)
    fake, repo = e2e["fake"], e2e["world"].full_name
    await pr_comment(e2e)
    await dispatch_all(e2e)
    assert failed and len(fake.runs(repo)) == 1  # created, then the local transaction failed
    await let_time_pass(e2e)
    await dispatch_all(e2e)

    assert len(fake.runs(repo)) == 1, "the retry created a second check run"


@pytest.mark.asyncio
async def test_a_lost_comment_response_does_not_post_a_second_acknowledgement(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    fake, repo = e2e["fake"], e2e["world"].full_name
    fake.faults.append(Fault("POST", rf"/repos/{repo}/issues/2/comments", 502, times=1, after=True))
    await pr_comment(e2e)
    await dispatch_all(e2e)
    await let_time_pass(e2e)
    await dispatch_all(e2e)
    assert len(acks(e2e)) == 1, f"{len(acks(e2e))} acknowledgement comments"


@pytest.mark.asyncio
async def test_a_saved_check_run_is_updated_not_recreated(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    # Case B: the ID was saved; later events and redeliveries update that run.
    fake, repo = e2e["fake"], e2e["world"].full_name
    await pr_comment(e2e)
    await dispatch_all(e2e)
    [event] = await status_events(e2e)
    async with e2e["maker"]() as s:
        link = (await s.execute(select(TaskGitHubLink))).scalar_one()
        s.add(OutboxEvent(event_type="GITHUB_STATUS_UPDATE", aggregate_id=link.task_id,
                          payload={**event.payload, "event": "JOB_STARTED", "job_status": "RUNNING"}))  # fmt: skip
        s.add(OutboxEvent(event_type="GITHUB_STATUS_UPDATE", aggregate_id=link.task_id,
                          payload=dict(event.payload)))  # the acceptance, delivered again  # fmt: skip
        await s.commit()
    await dispatch_all(e2e)

    runs = fake.runs(repo)
    assert len(runs) == 1 and runs[0]["status"] == "in_progress"
    assert len(acks(e2e)) == 1


@pytest.mark.asyncio
async def test_two_dispatchers_racing_create_one_check_run_and_one_comment(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    import asyncio

    from app.outbox_dispatcher import dispatch_batch
    from app.services.queue_service import QueueService

    fake, repo = e2e["fake"], e2e["world"].full_name
    await pr_comment(e2e)

    async def one_dispatcher() -> None:
        async with e2e["maker"]() as s:
            for _ in range(3):
                await dispatch_batch(session=s, queue=QueueService())

    await asyncio.gather(one_dispatcher(), one_dispatcher())
    await dispatch_all(e2e)
    assert len(fake.runs(repo)) == 1 and len(acks(e2e)) == 1
    [event] = await status_events(e2e)
    assert event.published_at is not None and event.attempts == 0


@pytest.mark.asyncio
async def test_a_github_refusal_is_not_retried_but_can_be_requeued(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    from app.outbox_admin import requeue

    fake, repo = e2e["fake"], e2e["world"].full_name
    refusal = Fault("POST", rf"/repos/{repo}/check-runs", 403)  # the App lost "checks: write"
    fake.faults.append(refusal)
    await pr_comment(e2e)
    await dispatch_all(e2e)
    for _ in range(3):
        await let_time_pass(e2e)
        await dispatch_all(e2e)
    [event] = await status_events(e2e)
    assert event.failed_at is not None and event.attempts == 1 and event.last_error_category == "permanent"
    assert refusal.hits == 1  # asked once, never again by itself

    fake.faults.remove(refusal)  # the permission is granted again
    async with e2e["maker"]() as s:
        assert await requeue(s, [event.id], by="ops", reason="checks permission restored") == {event.id: True}
    await dispatch_all(e2e)
    assert [r["status"] for r in fake.runs(repo)] == ["queued"] and len(acks(e2e)) == 1


@pytest.mark.asyncio
async def test_the_acknowledgement_is_found_in_a_busy_thread(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    # 150 earlier comments: the managed comment is beyond the first page of 100.
    from datetime import UTC, datetime, timedelta

    fake, world = e2e["fake"], e2e["world"]
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    thread = fake.comments.setdefault((world.full_name, 2), [])
    for n in range(150):
        thread.append({"id": 8_000_000 + n, "body": f"discussion {n}", "html_url": "", "created_at": old,
                       "updated_at": old, "user": {"login": "someone", "id": 1, "type": "User"}})  # fmt: skip
    await pr_comment(e2e)
    await dispatch_all(e2e)
    assert len(acks(e2e)) == 1

    [event] = await status_events(e2e)
    async with e2e["maker"]() as s:  # the acceptance is delivered again (a requeue, a redelivery)
        s.add(OutboxEvent(event_type="GITHUB_STATUS_UPDATE", aggregate_id=event.aggregate_id, payload=dict(event.payload)))
        await s.commit()
    await dispatch_all(e2e)
    assert len(acks(e2e)) == 1, "a second acknowledgement in a long thread"
