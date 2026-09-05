import pytest

from app.github.idempotency import DuplicateWebhookDelivery, WebhookDeliveryStore


def test_first_delivery_accepted():
    store = WebhookDeliveryStore()
    store.mark_seen("1") # Should pass

def test_duplicate_delivery_fails():
    store = WebhookDeliveryStore()
    store.mark_seen("1")
    with pytest.raises(DuplicateWebhookDelivery):
        store.mark_seen("1")

def test_different_deliveries_independent():
    store = WebhookDeliveryStore()
    store.mark_seen("1")
    store.mark_seen("2") # Passes

def test_bounded_store_behavior():
    store = WebhookDeliveryStore(capacity=2)
    store.mark_seen("1")
    store.mark_seen("2")
    store.mark_seen("3") # Evicts 1

    # 1 should be accepted again because it was evicted
    store.mark_seen("1")
