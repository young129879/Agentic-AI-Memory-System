"""
Read-only access to memory the model was told about but not given.

Injection lists past topics as an index -- label plus 200 characters -- and
tells the model it can fetch one in full. These are the endpoints that make
that promise real.

Read-only on purpose. The model can pull context in; it cannot rewrite what
it remembers.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

# A block can be very long. This is what one fetch is allowed to return, so
# expanding a topic cannot blow the caller's context window.
MAX_BLOCK_CHARS = 20_000
MAX_TURNS_RETURNED = 40


def _clip(text: str, limit: int) -> str:
    """
    Cut to `limit` characters *including* the ellipsis, so a caller enforcing
    a budget gets what it asked for. Splits on code points, never bytes, so
    multi-byte characters are not cut in half.
    """
    if not text:
        return ""
    chars = list(text)
    if len(chars) <= limit:
        return text
    suffix = "..."
    keep = max(limit - len(suffix), 0)
    return "".join(chars[:keep]) + suffix


def create_router(get_service, prefix: str = "/memory") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["bridge"])

    def _storage():
        return get_service().client.engine.storage

    @router.get("/block/{block_id}")
    async def get_block(
        block_id: str,
        session_id: Optional[str] = Query(
            None,
            description="Caller's session. Required to read a block; a block "
                        "belonging to another session returns 404.",
        ),
    ) -> Dict[str, Any]:
        """
        Full text of one bridge block.

        A block id is not a capability. Ownership is checked rather than
        assumed, and a foreign block is reported as missing rather than
        forbidden -- confirming it exists would leak that another session is
        working on something.
        """
        storage = _storage()

        if session_id:
            from hmlr.memory.persistence.ledger_store import LedgerStore
            owner = LedgerStore._session_for_block(storage.conn, block_id)
            if owner != session_id:
                logger.warning(
                    f"Blocked cross-session block read: {block_id} "
                    f"owned by {owner}, requested by {session_id}"
                )
                raise HTTPException(status_code=404, detail="Block not found")

        block = storage.get_bridge_block_full(block_id)
        if not block:
            raise HTTPException(status_code=404, detail="Block not found")

        turns = block.get("turns") or []
        truncated_turns = len(turns) > MAX_TURNS_RETURNED
        if truncated_turns:
            # Keep the most recent: a resumed topic is usually continued from
            # where it stopped, not from its opening.
            turns = turns[-MAX_TURNS_RETURNED:]

        rendered = []
        for t in turns:
            rendered.append({
                "turn_id": t.get("turn_id"),
                "timestamp": t.get("timestamp"),
                "user": _clip(t.get("user_message", ""), 4000),
                "assistant": _clip(t.get("assistant_response", ""), 4000),
            })

        payload = {
            "block_id": block_id,
            "topic_label": block.get("topic_label", ""),
            "summary": block.get("summary", ""),
            "status": block.get("status"),
            "keywords": block.get("keywords", []),
            "open_loops": block.get("open_loops", []),
            "decisions_made": block.get("decisions_made", []),
            "turn_count": len(block.get("turns") or []),
            "turns": rendered,
            "turns_truncated": truncated_turns,
            "facts": storage.get_facts_for_block(block_id),
        }

        total = sum(len(str(v)) for v in payload.values())
        if total > MAX_BLOCK_CHARS:
            payload["turns"] = payload["turns"][-10:]
            payload["turns_truncated"] = True
            logger.info(f"Block {block_id} trimmed to fit {MAX_BLOCK_CHARS} chars")

        return payload

    @router.get("/blocks")
    async def list_blocks(
        session_id: str = Query(..., description="Only this session's blocks"),
    ) -> Dict[str, Any]:
        """Topic index for a session: labels and short summaries, no turns."""
        storage = _storage()
        blocks = storage.get_active_bridge_blocks(session_id)
        return {
            "session_id": session_id,
            "count": len(blocks),
            "blocks": [{
                "block_id": b.get("block_id"),
                "topic_label": b.get("content", {}).get("topic_label", ""),
                "summary": _clip(b.get("content", {}).get("summary", ""), 200),
                "status": b.get("status"),
                "created_at": b.get("created_at"),
            } for b in blocks],
        }

    @router.get("/search")
    async def search_facts(
        q: str = Query(..., min_length=1, description="Keyword"),
        session_id: Optional[str] = Query(None),
        limit: int = Query(10, ge=1, le=50),
    ) -> Dict[str, Any]:
        """
        Keyword search over facts.

        Complements the injected snapshot, which is fixed for the session:
        when the conversation moves somewhere the snapshot did not anticipate,
        this is how the model reaches the rest.
        """
        scrubber = getattr(get_service().client.engine, "fact_scrubber", None)
        if scrubber is None:
            return {"query": q, "count": 0, "facts": []}

        facts = scrubber.query_facts(q, limit=limit, session_id=session_id)
        return {
            "query": q,
            "count": len(facts),
            "facts": [{
                "key": f.key,
                "value": f.value,
                "category": f.category,
                "evidence": _clip(f.evidence_snippet or "", 300),
                "block_id": f.source_block_id,
            } for f in facts],
        }

    return router
