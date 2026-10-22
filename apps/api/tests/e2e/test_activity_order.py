"""The activity history shows a decision before the status change it caused.

A decision and its transition are written in one transaction.  The approve
and reject paths stamp both with the transaction's now(), so they tie; the
expiry path stamps the decision with the clock, a moment later.  Sorting by
timestamp alone showed the task leaving "Awaiting plan approval" before
anyone had decided.  Same harness as test_comment_to_approval.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.db.models import TaskApproval, TaskStatusEnum
from tests.e2e.test_comment_to_approval import e2e  # noqa: F401
from tests.e2e.test_reviewer_revocation import waiting_for_approval


def kinds(body: dict[str, Any]) -> list[str]:
    """Each item as "kind:detail", e.g. "status:WAITING_APPROVAL>QUEUED" or "approval_decided:APPROVED"."""
    out = []
    for item in body["items"]:
        if item["kind"] == "status":
            out.append(f"status:{item['from_status']}>{item['to_status']}")
        elif item["kind"].startswith("approval"):
            out.append(f"{item['kind']}:{item['approval_status']}")
        else:
            out.append(item["kind"])
    return out


async def activity(env: dict[str, Any], task_id: str) -> list[str]:
    response = await env["api"].get(
        f"/api/v1/tasks/{task_id}/activity", headers=env["world"].users["lead"][1]
    )
    assert response.status_code == 200, response.text
    assert response.json()["partial"] is False
    return kinds(response.json())


@pytest.mark.parametrize(("decision", "outcome"), [("approve", "QUEUED"), ("reject", "CANCELLED")])
@pytest.mark.asyncio
async def test_a_decision_comes_before_the_transition_it_caused(
    e2e: dict[str, Any],  # noqa: F811
    decision: str,
    outcome: str,
) -> None:
    task_id, approval_id = await waiting_for_approval(e2e)
    response = await e2e["api"].post(f"/api/v1/tasks/{task_id}/{decision}", json={"approval_id": approval_id},
                                     headers=e2e["world"].users["lead"][1])  # fmt: skip
    assert response.status_code == 200, response.text

    items = await activity(e2e, task_id)
    verdict = "approval_decided:APPROVED" if decision == "approve" else "approval_decided:REJECTED"
    assert items[-2:] == [verdict, f"status:WAITING_APPROVAL>{outcome}"], items
    assert items.index("approval_requested:PENDING") < items.index(verdict)


@pytest.mark.asyncio
async def test_an_expiry_comes_before_the_cancellation_it_caused(e2e: dict[str, Any]) -> None:  # noqa: F811
    task_id, approval_id = await waiting_for_approval(e2e)
    async with e2e["maker"]() as session:
        await session.execute(
            update(TaskApproval)
            .where(TaskApproval.id == approval_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    # Reading the task records the expiry (and cancels the task) in one transaction.
    detail = await e2e["api"].get(f"/api/v1/tasks/{task_id}", headers=e2e["world"].users["lead"][1])
    assert detail.json()["status"] == TaskStatusEnum.CANCELLED.value

    items = await activity(e2e, task_id)
    assert items[-2:] == ["approval_decided:EXPIRED", "status:WAITING_APPROVAL>CANCELLED"], items
