"""
Server-side store for in-flight retrieval state.

recall() produces state that ingest() needs (turn id, chunks, routed block)
but which cannot cross the wire: chunks and MemoryCandidate are live objects,
and the block id is not something a caller should be able to forge.

So the state stays here and the caller gets an opaque handle. Entries are
short-lived by nature -- one request/response round trip -- so they expire
rather than accumulate when a caller never returns.
"""

import logging
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_ENTRIES = 10_000


class ContextStore:
    """
    TTL map from handle to RecallResult.

    Guarded by a lock because uvicorn serves requests from a thread pool and
    two windows can be mid-turn at the same time.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[float, object]] = {}

    def put(self, value: object) -> str:
        handle = secrets.token_urlsafe(16)
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if len(self._entries) >= self._max:
                # Drop the oldest rather than refuse the turn: a stale handle
                # degrades one reply, a rejected recall breaks the request.
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
                logger.warning("Context store full; evicted oldest entry")
            self._entries[handle] = (now, value)
        return handle

    def take(self, handle: Optional[str]) -> Optional[object]:
        """Retrieve and remove. A handle is valid for exactly one ingest."""
        if not handle:
            return None
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            entry = self._entries.pop(handle, None)
        if entry is None:
            logger.warning(f"Unknown or expired context handle: {handle[:8]}...")
            return None
        return entry[1]

    def __len__(self) -> int:
        with self._lock:
            self._sweep_locked(time.time())
            return len(self._entries)

    def _sweep_locked(self, now: float) -> None:
        cutoff = now - self._ttl
        expired = [k for k, (ts, _) in self._entries.items() if ts < cutoff]
        for k in expired:
            del self._entries[k]
        if expired:
            logger.debug(f"Swept {len(expired)} expired context entries")
