import contextvars
from collections.abc import Callable
from typing import Any


class OwnershipLostError(Exception):
    """Raised when an operation attempts to mutate state after job ownership is lost."""
    pass

_ownership_verifier: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar("ownership_verifier", default=None)
_async_ownership_verifier: contextvars.ContextVar[Callable[[], Any] | None] = contextvars.ContextVar("async_ownership_verifier", default=None)

def set_ownership_verifier(verifier: Callable[[], None]) -> contextvars.Token:
    """Set the current thread's ownership verifier."""
    return _ownership_verifier.set(verifier)

def reset_ownership_verifier(token: contextvars.Token) -> None:
    """Reset the ownership verifier to its previous state."""
    _ownership_verifier.reset(token)

def set_async_ownership_verifier(verifier: Callable[[], Any]) -> contextvars.Token:
    """Set the current thread's async ownership verifier."""
    return _async_ownership_verifier.set(verifier)

def reset_async_ownership_verifier(token: contextvars.Token) -> None:
    """Reset the async ownership verifier to its previous state."""
    _async_ownership_verifier.reset(token)

def verify_ownership() -> None:
    """Verify that execution ownership is still valid using local flag. Raises OwnershipLostError if not."""
    verifier = _ownership_verifier.get()
    if verifier:
        verifier()

async def verify_async_ownership() -> None:
    """Verify authoritative DB execution ownership. Raises OwnershipLostError if not."""
    verifier = _async_ownership_verifier.get()
    if verifier:
        await verifier()
