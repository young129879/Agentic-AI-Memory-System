"""
Per-session cache for the injected memory block.

Two problems, one fix.

Cost: recall() runs two LLM calls -- routing and 2-key filtering -- so doing
it every turn multiplies the conversation's own token spend.

Prompt caching: Anthropic caches on an exact prefix match. The system prompt
is the prefix. If the injected block changes by a single character between
turns, the cache misses and the entire prompt is billed at full rate. Fresh
retrieval each turn produces subtly different text -- reordered candidates, a
new fact, a summary that grew -- so it defeats caching precisely when a
conversation is long enough for caching to matter.

So the block is computed once per session and reused verbatim. Freshness is
not lost: the model reaches new memory through the bridge endpoints, which is
why they exist.

Routing still has to run every turn -- it decides which block a turn belongs
to and is a write -- so only the rendered text is cached, not recall itself.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 7200
DEFAULT_MAX_SESSIONS = 1000


class InjectionCache:
    """
    session_id -> rendered memory block.

    Entries are immutable once written. A refresh replaces the whole entry so
    a half-updated block can never be served.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_sessions: int = DEFAULT_MAX_SESSIONS):
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._lock = threading.Lock()
        # session_id -> (written_at, block_text)
        self._entries: Dict[str, Tuple[float, str]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, session_id: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                self.misses += 1
                return None
            written_at, block = entry
            if now - written_at > self._ttl:
                del self._entries[session_id]
                self.misses += 1
                return None
            self.hits += 1
            return block

    def put(self, session_id: str, block: str) -> None:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            if session_id not in self._entries and len(self._entries) >= self._max:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
                logger.info(f"Injection cache full; evicted session {oldest}")
            self._entries[session_id] = (now, block)

    def invalidate(self, session_id: str) -> None:
        """Force a rebuild on the next turn."""
        with self._lock:
            self._entries.pop(session_id, None)

    def stats(self) -> Dict[str, float]:
        total = self.hits + self.misses
        with self._lock:
            live = len(self._entries)
        return {
            "sessions": live,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }

    def _sweep_locked(self, now: float) -> None:
        cutoff = now - self._ttl
        for key in [k for k, (ts, _) in self._entries.items() if ts < cutoff]:
            del self._entries[key]
