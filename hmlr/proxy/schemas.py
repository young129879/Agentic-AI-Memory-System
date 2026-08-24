"""
Wire formats for the memory service.

RecallResult is deliberately not returned verbatim: it carries live objects
(chunks, MemoryCandidate) that do not serialise, and its continuation state
is meaningless to the caller. Instead recall returns rendered memory plus an
opaque `context` blob which the caller hands back to ingest unchanged.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RecallRequest(BaseModel):
    query: str = Field(..., description="The user message to retrieve memory for")
    session_id: str = Field(
        "default_session",
        description="Isolates one agent window from another",
    )


class BlockIndexEntry(BaseModel):
    """A topic label and short summary. Full text is fetched on demand."""

    block_id: Optional[str] = None
    topic_label: str = "Unknown"
    summary: str = ""
    status: Optional[str] = None


class RecallResponse(BaseModel):
    session_id: str
    turn_id: str
    block_id: Optional[str]
    is_new_topic: bool

    dossiers: List[Dict[str, Any]] = []
    facts: List[Dict[str, Any]] = []
    block_facts: List[Dict[str, Any]] = []
    open_loops: List[str] = []
    block_index: List[BlockIndexEntry] = []
    memory_count: int = 0

    context: str = Field(
        ...,
        description=(
            "Opaque handle for the retrieval state of this turn. "
            "Pass it back to /memory/ingest unchanged."
        ),
    )

    degraded: bool = Field(
        False,
        description="Memory was unavailable; the reply can still be generated",
    )
    error: Optional[str] = None


class IngestRequest(BaseModel):
    user_message: str
    assistant_reply: str
    session_id: str = "default_session"
    context: Optional[str] = Field(
        None,
        description=(
            "The handle returned by /memory/recall. Omitting it still logs "
            "the turn, but it cannot be attached to its bridge block."
        ),
    )


class IngestResponse(BaseModel):
    ok: bool = Field(..., description="True if the turn was attached to a block")
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    db_path: Optional[str] = None
    sessions_cached: int = 0
    injection_cache: Dict[str, Any] = Field(
        default_factory=dict,
        description="Hit rate for the per-session injected memory block. "
                    "A low rate means the upstream prompt prefix is changing "
                    "between turns and prompt caching is being defeated.",
    )
    auto_gardener: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Status of the automatic idle-block archiver.",
    )
