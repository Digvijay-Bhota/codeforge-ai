"""Redaction of credentials from text that is persisted, logged or displayed.

Failure messages come from third-party libraries (git, the OpenAI SDK, httpx,
database drivers), so they may quote a token, a key or a connection string.
``redact_secrets`` replaces the credential-bearing parts of the well-known
shapes and leaves the rest of the message readable.  It is a safety net for
text we do not control, not a substitute for never formatting a secret in the
first place.

``redact_query`` removes the values of sensitive query parameters (OAuth
authorization codes and state, tokens) from a URL or request target, for
access logs.
"""

from __future__ import annotations

import re

REDACTED = "***"

# Query parameters whose values are credentials or one-time secrets.  Matched
# case-insensitively, by exact name.
SENSITIVE_QUERY_PARAMS = frozenset({
    "code",
    "state",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "client_secret",
    "password",
    "api_key",
    "key",
    "signature",
    "sig",
})

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys (whole block, any key type).
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.DOTALL),
        f"[PRIVATE KEY {REDACTED}]",
    ),
    # Credentials embedded in URLs: scheme://user:password@host, x-access-token:TOKEN@.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]*:[^\s/@]+@"), rf"\1{REDACTED}@"),
    (re.compile(r"(?i)x-access-token:[^\s@]+"), f"x-access-token:{REDACTED}"),
    # HTTP authorization values.
    (re.compile(r"(?i)\b(authorization:\s*)(?:[A-Za-z]+\s+)?\S+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    # GitHub tokens (classic, fine-grained, installation, OAuth, refresh).
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    # OpenAI-style API keys (sk-..., sk-proj-...), including partly masked ones.
    (re.compile(r"\bsk-[A-Za-z0-9_*-]{8,}"), f"sk-{REDACTED}"),
    # JSON Web Tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
)

_QUERY_PARAM = re.compile(r"(?P<sep>[?&;])(?P<name>[^=&;#\s]+)=(?P<value>[^&;#\s]*)")


def redact_secrets(text: str) -> str:
    """Return *text* with credential-shaped substrings replaced by ``***``."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return redact_query(text)


def redact_query(text: str) -> str:
    """Replace the values of sensitive query parameters in *text* with ``***``.

    Works on a bare request target (``/callback?code=...&state=...``) or on any
    text that contains one, such as a log line.
    """

    def _sub(match: re.Match[str]) -> str:
        if match.group("name").lower() in SENSITIVE_QUERY_PARAMS and match.group("value"):
            return f"{match.group('sep')}{match.group('name')}={REDACTED}"
        return match.group(0)

    return _QUERY_PARAM.sub(_sub, text)

