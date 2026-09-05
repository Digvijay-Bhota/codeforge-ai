"""Webhook delivery deduplication store."""

from collections import OrderedDict


class DuplicateWebhookDelivery(Exception):
    pass

class WebhookDeliveryStore:
    """In-memory bounded store for webhook idempotency.

    This is for Phase 6C development only. In production, a Redis or
    PostgreSQL-backed store with TTL should be used.
    """

    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        # OrderedDict used as an LRU cache
        self._seen: OrderedDict[str, bool] = OrderedDict()

    def mark_seen(self, delivery_id: str) -> None:
        """Mark a delivery as seen, evicting the oldest if capacity is reached.

        Raises DuplicateWebhookDelivery if already seen.
        """
        if delivery_id in self._seen:
            raise DuplicateWebhookDelivery(f"Delivery {delivery_id} already processed")

        self._seen[delivery_id] = True

        if len(self._seen) > self.capacity:
            self._seen.popitem(last=False)  # pop FIFO
