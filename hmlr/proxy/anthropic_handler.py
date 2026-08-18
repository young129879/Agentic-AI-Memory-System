"""
Anthropic Messages endpoint.

Sits between an agent and api.anthropic.com. Per turn:

    recall memory -> append to system -> forward -> stream back -> ingest

Two rules shape the whole file:

Memory must never break the conversation. Every memory step is wrapped so
that a failure degrades to a plain proxy rather than a 500. The user gets
their answer; they just do not get memory that turn.

The client must never wait for memory. Retrieval happens before the upstream
call because its result has to go into the request, but persistence happens
after the response is fully delivered, off the request path.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Set, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .injection import build_memory_block
from .injection_cache import InjectionCache
from .protocol import anthropic as ap
from .streaming import AnthropicStreamTap, text_from_non_streaming

logger = logging.getLogger(__name__)

# Hop-by-hop and routing headers that must not be replayed upstream.
# content-length in particular: injection changes the body size.
SKIP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "accept-encoding",
    "transfer-encoding", "x-session-id", "x-conversation-id", "x-hmlr-session",
}
SKIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
}

# Writes are fire-and-forget; asyncio only holds a weak reference to a task,
# so without a strong one here the garbage collector can cancel a write
# mid-flight and the turn silently vanishes.
_pending_writes: Set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _pending_writes.add(task)
    task.add_done_callback(_pending_writes.discard)


async def flush_pending_writes(timeout: float = 10.0) -> int:
    """Wait for in-flight memory writes. Called on shutdown."""
    if not _pending_writes:
        return 0
    pending = list(_pending_writes)
    done, _ = await asyncio.wait(pending, timeout=timeout)
    return len(done)


def create_router(get_service, upstream_url: str, upstream_key: Optional[str],
                  bridge_url: Optional[str] = None,
                  timeout: float = 600.0,
                  injection_cache: Optional[InjectionCache] = None) -> APIRouter:
    """
    Args:
        get_service: returns the MemoryService. A callable rather than the
            object itself because the service is built during startup, after
            routes are registered.
        upstream_url: real Anthropic base, e.g. https://api.anthropic.com
        upstream_key: API key to use upstream; when None the client's own
            key is passed through, which is what a local setup wants
        bridge_url: base URL for block fetches, advertised to the model
        injection_cache: pins the rendered memory block per session so the
            upstream prompt prefix stays byte-identical and stays cacheable
    """
    service_cache = injection_cache or InjectionCache()
    router = APIRouter()

    async def _recall(query: str, session_id: str) -> Optional[Dict[str, Any]]:
        if not query:
            return None
        try:
            service = get_service()
            result = await service.client.recall(query, session_id=session_id)
            handle = service.contexts.put(result)
            payload = result.to_dict()
            payload["context"] = handle
            return payload
        except Exception as e:
            logger.error(f"recall failed, continuing without memory: {e}",
                         exc_info=True)
            return None

    async def _memory_for_turn(query: str, session_id: str
                               ) -> Tuple[str, Optional[str]]:
        """
        Return (block_to_inject, ingest_handle) for this turn.

        recall() always runs: routing decides which bridge block the turn
        belongs to, and it is a write. Only the rendered text is cached,
        because that text is the cached prefix of the upstream prompt and
        must be byte-identical across the session.
        """
        recall = await _recall(query, session_id)
        handle = recall.get("context") if recall else None

        cached = service_cache.get(session_id)
        if cached is not None:
            return cached, handle

        block = build_memory_block(recall, bridge_url=bridge_url) if recall else ""
        # Cached even when empty: an early turn with no memory yet would
        # otherwise retry rendering on every subsequent turn, and injecting
        # memory partway through would invalidate the prefix anyway.
        service_cache.put(session_id, block)
        if block:
            logger.info(
                f"[{session_id}] built memory block ({len(block)} chars), "
                f"pinned for this session"
            )
        return block, handle

    async def _ingest(handle: Optional[str], query: str, reply: str,
                      session_id: str) -> None:
        if not reply:
            return
        try:
            service = get_service()
            ctx = service.contexts.take(handle) if handle else None
            await service.client.ingest(query, reply, ctx=ctx,
                                        session_id=session_id)
        except Exception as e:
            logger.error(f"ingest failed: {e}", exc_info=True)

    @router.post("/v1/messages")
    async def messages(request: Request):
        raw = await request.body()
        headers = dict(request.headers)

        try:
            import json
            body = json.loads(raw)
        except Exception:
            # Not our shape to interpret; hand it upstream untouched.
            body = None

        fwd_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in SKIP_REQUEST_HEADERS
        }
        if upstream_key:
            fwd_headers["x-api-key"] = upstream_key
            fwd_headers.pop("authorization", None)

        session_id = "default_session"
        query = ""
        handle = None

        if isinstance(body, dict):
            session_id = ap.session_hint(body, headers)
            query = ap.last_user_text(body)

            block, handle = await _memory_for_turn(query, session_id)
            if block:
                body = ap.append_to_system(body, block)
            raw = json.dumps(body).encode("utf-8")

        streaming = isinstance(body, dict) and ap.is_streaming(body)
        url = f"{upstream_url.rstrip('/')}/v1/messages"

        if not streaming:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream = await client.post(url, content=raw, headers=fwd_headers)

            if upstream.status_code == 200 and query:
                try:
                    reply = text_from_non_streaming(upstream.json())
                    _spawn(_ingest(handle, query, reply, session_id))
                except Exception as e:
                    logger.error(f"Could not read non-streaming reply: {e}")

            return JSONResponse(
                status_code=upstream.status_code,
                content=upstream.json() if upstream.content else None,
                headers={k: v for k, v in upstream.headers.items()
                         if k.lower() not in SKIP_RESPONSE_HEADERS},
            )

        async def relay():
            tap = AnthropicStreamTap()
            client = httpx.AsyncClient(timeout=timeout)
            try:
                async with client.stream("POST", url, content=raw,
                                         headers=fwd_headers) as upstream:
                    if upstream.status_code != 200:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                        return
                    async for chunk in upstream.aiter_bytes():
                        tap.feed(chunk)
                        yield chunk          # forwarded before it is parsed
                tap.finish()
                if query and tap.assistant_text:
                    _spawn(_ingest(handle, query, tap.assistant_text, session_id))
            finally:
                await client.aclose()

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return router
