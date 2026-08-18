"""
Retry for memory writes.

Persistence is fire-and-forget: it runs after the response has been
delivered, so nobody is waiting on it and nobody sees it fail. That makes a
transient error -- a locked database, a momentary stall -- a silently lost
turn.

Only transient failures are retried. A bug in the write path will fail
identically three times, so retrying it just delays the log line.
"""

import asyncio
import logging
import random
import sqlite3
from typing import Awaitable, Callable, Optional, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 4.0

# SQLite contention under concurrent sessions is the expected transient
# failure here; OSError covers a filesystem hiccup.
TRANSIENT_ERRORS: Tuple[Type[BaseException], ...] = (
    sqlite3.OperationalError,
    sqlite3.DatabaseError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        # "database is locked" clears on its own; "no such column" will not.
        message = str(exc).lower()
        return "locked" in message or "busy" in message
    return isinstance(exc, TRANSIENT_ERRORS)


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    label: str = "operation",
) -> Optional[T]:
    """
    Run `operation`, retrying transient failures with exponential backoff.

    Returns None when every attempt fails. Raising instead would surface in a
    background task nobody awaits, so the failure is logged and swallowed
    deliberately.
    """
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            # Shutdown, not failure. Do not swallow it.
            raise
        except Exception as e:
            last_error = e
            if not _is_transient(e):
                logger.error(f"{label} failed permanently: {e}", exc_info=True)
                return None
            if attempt == attempts:
                break
            # Jitter so that several sessions failing on the same lock do not
            # all wake at the same instant and collide again.
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay *= 0.5 + random.random()
            logger.warning(
                f"{label} attempt {attempt}/{attempts} failed ({e}); "
                f"retrying in {delay:.2f}s"
            )
            await asyncio.sleep(delay)

    logger.error(f"{label} failed after {attempts} attempts: {last_error}")
    return None
