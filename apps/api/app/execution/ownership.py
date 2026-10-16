import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


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


async def run_until_ownership_lost(work: Awaitable[T], ownership_lost: asyncio.Event) -> T:
    """Await *work* unless *ownership_lost* is set first.

    When ownership is lost, *work* is cancelled and its cleanup is awaited
    (for a test executor, that is what kills the process group) before
    :class:`OwnershipLostError` is raised.  Any result *work* produces once
    ownership is lost is discarded.  Cancelling the caller cancels *work* too.

    *ownership_lost* must belong to the job that owns *work*, so a lost lease
    only ever cancels that job's execution.
    """
    task = asyncio.ensure_future(work)
    lost = asyncio.ensure_future(ownership_lost.wait())
    try:
        await asyncio.wait({task, lost}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task})
        raise
    finally:
        lost.cancel()

    if not ownership_lost.is_set():
        return task.result()

    task.cancel()
    await asyncio.wait({task})  # wait for cleanup without re-raising its outcome
    if not task.cancelled():
        task.exception()  # mark retrieved; a stale result or error is discarded
    raise OwnershipLostError("Job ownership lost; active execution cancelled")
