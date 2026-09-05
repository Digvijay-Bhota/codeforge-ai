import hashlib
import hmac
import json

import pytest

from app.github.webhooks import (
    UnsupportedWebhookEvent,
    parse_webhook_payload,
    verify_signature,
)


def get_signature(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()

def test_valid_signature_accepted():
    body = b"hello world"
    sig = get_signature("secret123", body)
    assert verify_signature(body, sig, "secret123") is True

def test_invalid_signature_rejected():
    body = b"hello world"
    sig = get_signature("wrong_secret", body)
    assert verify_signature(body, sig, "secret123") is False

def test_missing_signature_rejected():
    assert verify_signature(b"hello", "", "secret123") is False

def test_malformed_signature_rejected():
    assert verify_signature(b"hello", "md5=123", "secret123") is False

def test_modified_payload_rejected():
    body1 = b"hello world"
    sig = get_signature("secret", body1)
    body2 = b"hello world modified"
    assert verify_signature(body2, sig, "secret") is False

def test_parse_issues_opened():
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "r", "owner": {"login": "o"}, "full_name": "o/r", "default_branch": "main"},
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"}
    }
    raw = json.dumps(payload).encode("utf-8")
    ev = parse_webhook_payload(raw, "issues", "del-1")
    assert ev.event_type == "issues"
    assert ev.issue.number == 1
    assert ev.issue.title == "A bug"
    assert ev.issue.body == "Fix it please"

def test_parse_oversized_payload_bounded():
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "r", "owner": {"login": "o"}, "full_name": "o/r", "default_branch": "main"},
        "issue": {"number": 1, "title": "A" * 500, "body": "B" * 20000}
    }
    raw = json.dumps(payload).encode("utf-8")
    ev = parse_webhook_payload(raw, "issues", "del-1")
    assert len(ev.issue.title) == 255
    assert len(ev.issue.body) == 10000

def test_parse_pull_request_synchronize():
    payload = {
        "action": "synchronize",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "r", "owner": {"login": "o"}, "full_name": "o/r", "default_branch": "main"},
        "pull_request": {"number": 1, "title": "PR", "body": "Body", "head": {"ref": "feature"}, "base": {"ref": "main"}}
    }
    raw = json.dumps(payload).encode("utf-8")
    ev = parse_webhook_payload(raw, "pull_request", "del-2")
    assert ev.event_type == "pull_request"
    assert ev.pull_request.head_ref == "feature"

def test_unsupported_event_ignored():
    payload = {
        "action": "deleted",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "r", "owner": {"login": "o"}, "full_name": "o/r", "default_branch": "main"},
    }
    raw = json.dumps(payload).encode("utf-8")
    with pytest.raises(UnsupportedWebhookEvent):
        parse_webhook_payload(raw, "issues", "del-1")
