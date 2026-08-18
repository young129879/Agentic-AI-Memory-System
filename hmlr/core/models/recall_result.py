"""
Split-phase conversation types.

`_handle_chat` does retrieve -> generate -> persist in one call, which works
for a CLI but not for a proxy: the proxy must own the generation step so the
agent's own model, streaming format and tool calls are preserved.

RecallResult is what makes that split safe. Retrieval and persistence share
mutable state (the turn id, its chunks, the block it was routed into), so
rather than recomputing it -- which would re-run two LLM calls and could
route the same turn into a different block -- recall hands the state to the
caller and ingest takes it back.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RecallResult:
    """
    Everything retrieval produced for one turn.

    Two groups of fields:

    - Injectable memory (dossiers, memories, facts, ...) is what a caller
      renders into a system prompt.
    - Continuation state (turn_id, chunks, block_id, ...) is opaque to the
      caller and must be handed back to ingest() unchanged.
    """

    # --- injectable memory ---------------------------------------------
    dossiers: List[Dict[str, Any]] = field(default_factory=list)
    memories: List[Any] = field(default_factory=list)
    facts: List[Dict[str, Any]] = field(default_factory=list)
    block_facts: List[Dict[str, Any]] = field(default_factory=list)
    open_loops: List[str] = field(default_factory=list)
    block_index: List[Dict[str, Any]] = field(default_factory=list)

    # --- continuation state (pass back to ingest unchanged) ------------
    session_id: str = "default_session"
    day_id: str = ""
    turn_id: str = ""
    block_id: Optional[str] = None
    is_new_topic: bool = False
    chunks: List[Any] = field(default_factory=list)
    routing_decision: Dict[str, Any] = field(default_factory=dict)

    # --- diagnostics ----------------------------------------------------
    degraded: bool = False
    error: Optional[str] = None

    def is_empty(self) -> bool:
        """True when retrieval found nothing worth injecting."""
        return not (self.dossiers or self.memories or self.facts
                    or self.block_facts or self.open_loops)

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable view for HTTP responses and logging."""
        return {
            "session_id": self.session_id,
            "day_id": self.day_id,
            "turn_id": self.turn_id,
            "block_id": self.block_id,
            "is_new_topic": self.is_new_topic,
            "dossiers": self.dossiers,
            "facts": self.facts,
            "block_facts": self.block_facts,
            "open_loops": self.open_loops,
            "block_index": self.block_index,
            "memory_count": len(self.memories),
            "degraded": self.degraded,
            "error": self.error,
        }
