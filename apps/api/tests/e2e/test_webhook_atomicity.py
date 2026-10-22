"""A webhook that fails half way leaves nothing behind, so its redelivery still counts.

The delivery record (duplicate protection) and the outbox event (the work)
are written in one transaction.  If the delivery record survived a failure
of the outbox write, GitHub's redelivery of the same delivery ID would be
dropped as a duplicate and the comment silently lost.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.db.models import OutboxEvent, Task, WebhookDelivery
from app.db.repositories.outbox_repository import OutboxRepository
from tests.e2e.test_comment_to_approval import (  # noqa: F401
    COMMAND,
    count,
    deliver,
    dispatch_all,
    e2e,
    login,
)


@pytest.mark.asyncio
async def test_a_failed_outbox_write_rolls_back_the_delivery_record(e2e: dict[str, Any]) -> None:  # noqa: F811
    world = e2e["world"]
    raw = world.payload(login(world, "dev"), COMMAND)
    delivery_id = next(world.delivery_ids)

    with patch.object(OutboxRepository, "create_event", side_effect=ConnectionError("database went away")):
        with pytest.raises(ConnectionError):  # the API answers 500; GitHub records a failed delivery
            await deliver(e2e, raw, delivery_id=delivery_id)
    assert await count(e2e, WebhookDelivery) == 0
    assert await count(e2e, OutboxEvent) == 0

    # GitHub's redelivery (same delivery ID) is accepted and creates exactly one task.
    redelivered = await deliver(e2e, raw, delivery_id=delivery_id)
    assert redelivered.status_code == 202, redelivered.text
    await dispatch_all(e2e)
    assert await count(e2e, WebhookDelivery, WebhookDelivery.delivery_id == delivery_id) == 1
    assert await count(e2e, Task) == 1
