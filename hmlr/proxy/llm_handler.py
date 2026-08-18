"""
LLM proxy endpoints.

Sits between an agent and its model provider. Per turn:

    recall memory -> append to system -> forward -> stream back -> ingest

Two protocols, one pipeline. Anthropic Messages and OpenAI Chat Completions
differ only in where the system prompt lives and how SSE deltas are shaped,
so those differences are isolated in protocol adapters and the flow below is
written once.

Two rules shape the whole file:

Memory must never break the conversation. Every memory step is wrapped so
that a failure degrades to a plain proxy rather than a 500. The user gets
their answer; they just do not get memory that turn.

The client must never wait for memory. Retrieval happens before the upstream
call because its result has to go into the request, but persistence happens
after the response is fully delivered, off the request path.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional, Set, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .injection import build_memory_block
from .injection_cache import InjectionCache
from .protocol import anthropic as ap
from .protocol import openai as oa
from .retry import with_retry
from .streaming import (
    AnthropicStreamTap,
    OpenAIStreamTap,
    text_from_non_streaming,
)

logger = logging.getLogger(__name__)


def _sse_error(message: str) -> bytes:
    """An error the client can render once the stream has already started."""
    payload = json.dumps({"type": "error",
                          "error": {"type": "upstream_error", "message": message}})
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")

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


class ProtocolSpec:
    """What differs between the two wire formats."""

    def __init__(self, name: str, module, path: str, tap_factory: Callable,
                 non_streaming_text: Callable, auth_header: str):
        self.name = name
        self.module = module
        self.path = path
        self.tap_factory = tap_factory
        self.non_streaming_text = non_streaming_text
        self.auth_header = auth_header


ANTHROPIC = ProtocolSpec(
    name="anthropic",
    module=ap,
    path="/v1/messages",
    tap_factory=AnthropicStreamTap,
    non_streaming_text=text_from_non_streaming,
    auth_header="x-api-key",
)

OPENAI = ProtocolSpec(
    name="openai",
    module=oa,
    path="/v1/chat/completions",
    tap_factory=OpenAIStreamTap,
    non_streaming_text=oa.text_from_non_streaming,
    auth_header="authorization",
)


def create_router(get_service, upstream_url: str, upstream_key: Optional[str],
                  bridge_url: Optional[str] = None,
                  timeout: float = 600.0,
                  recall_timeout: float = 30.0,
                  injection_cache: Optional[InjectionCache] = None,
                  protocol: ProtocolSpec = ANTHROPIC) -> APIRouter:
    """
    Args:
        get_service: returns the MemoryService. A callable rather than the
            object itself because the service is built during startup, after
            routes are registered.
        upstream_url: real provider base, e.g. https://api.anthropic.com
        upstream_key: API key to use upstream; when None the client's own
            key is passed through, which is what a local setup wants
        bridge_url: base URL for block fetches, advertised to the model
        timeout: upstream request timeout
        recall_timeout: cap on retrieval, which sits in front of the user's
            request; past it the turn proceeds without memory
        injection_cache: pins the rendered memory block per session so the
            upstream prompt prefix stays byte-identical and stays cacheable
        protocol: ANTHROPIC or OPENAI
    """
    service_cache = injection_cache or InjectionCache()
    proto = protocol
    fmt = protocol.module
    router = APIRouter()

    async def _recall(query: str, session_id: str) -> Optional[Dict[str, Any]]:
        if not query:
            return None
        try:
            service = get_service()
            # Bounded: retrieval runs two LLM calls and sits in front of the
            # user's request, so a stalled provider would hold the turn open
            # indefinitely. Past the deadline the turn proceeds unaided.
            result = await asyncio.wait_for(
                service.client.recall(query, session_id=session_id),
                timeout=recall_timeout,
            )
            handle = service.contexts.put(result)
            payload = result.to_dict()
            payload["context"] = handle
            return payload
        except asyncio.TimeoutError:
            logger.warning(
                f"[{session_id}] recall exceeded {recall_timeout}s; "
                f"continuing without memory"
            )
            return None
        except Exception as e:
            logger.error(f"recall failed, continuing without memory: {e}",
                         exc_info=True)
            return None

    async def _ingest(handle: Optional[str], query: str, reply: str,
                      session_id: str) -> None:
        if not reply:
            return
        service = get_service()
        ctx = service.contexts.take(handle) if handle else None

        async def attempt():
            return await service.client.ingest(query, reply, ctx=ctx,
                                               session_id=session_id)

        await with_retry(attempt, label=f"ingest[{session_id}]")

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
                f"[{proto.name}][{session_id}] built memory block "
                f"({len(block)} chars), pinned for this session"
            )
        return block, handle

    @router.post(proto.path)
    async def completions(request: Request):
        raw = await request.body()
        headers = dict(request.headers)

        try:
            body = json.loads(raw)
        except Exception:
            # Not our shape to interpret; hand it upstream untouched.
            body = None

        fwd_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in SKIP_REQUEST_HEADERS
        }
        if upstream_key:
            if proto.auth_header == "authorization":
                fwd_headers["authorization"] = f"Bearer {upstream_key}"
                fwd_headers.pop("x-api-key", None)
            else:
                fwd_headers["x-api-key"] = upstream_key
                fwd_headers.pop("authorization", None)

        session_id = "default_session"
        query = ""
        handle = None

        if isinstance(body, dict):
            session_id = fmt.session_hint(body, headers)
            query = fmt.last_user_text(body)

            block, handle = await _memory_for_turn(query, session_id)
            if block:
                body = fmt.append_to_system(body, block)
            raw = json.dumps(body).encode("utf-8")

        streaming = isinstance(body, dict) and fmt.is_streaming(body)
        url = f"{upstream_url.rstrip('/')}{proto.path}"

        if not streaming:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    upstream = await client.post(url, content=raw,
                                                 headers=fwd_headers)
            except httpx.TimeoutException:
                logger.error(f"Upstream timed out after {timeout}s: {url}")
                return JSONResponse(
                    status_code=504,
                    content={"type": "error", "error": {
                        "type": "timeout_error",
                        "message": f"Upstream did not respond within {timeout}s",
                    }},
                )
            except httpx.HTTPError as e:
                # The provider is unreachable. Report it in the shape the
                # client already knows how to parse rather than as an
                # unhandled 500 from the proxy itself.
                logger.error(f"Upstream request failed: {e}")
                return JSONResponse(
                    status_code=502,
                    content={"type": "error", "error": {
                        "type": "upstream_error",
                        "message": str(e),
                    }},
                )

            if upstream.status_code == 200 and query:
                try:
                    reply = proto.non_streaming_text(upstream.json())
                    _spawn(_ingest(handle, query, reply, session_id))
                except Exception as e:
                    logger.error(f"Could not read non-streaming reply: {e}")

            try:
                content = upstream.json() if upstream.content else None
            except ValueError:
                # Some error paths return HTML or plain text; forwarding the
                # raw body is more useful than turning it into a proxy error.
                return Response(
                    status_code=upstream.status_code,
                    content=upstream.content,
                    media_type=upstream.headers.get("content-type"),
                )

            return JSONResponse(
                status_code=upstream.status_code,
                content=content,
                headers={k: v for k, v in upstream.headers.items()
                         if k.lower() not in SKIP_RESPONSE_HEADERS},
            )

        async def relay():
            tap = proto.tap_factory()
            client = httpx.AsyncClient(timeout=timeout)
            completed = False
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
                completed = True
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                logger.error(f"Upstream stream failed: {e}")
                # Mid-stream there is no status code left to set, so the
                # error is delivered as an SSE event the client can surface.
                yield _sse_error(f"Upstream stream failed: {e}")
            except asyncio.CancelledError:
                # The client hung up. Persist what was received rather than
                # discarding a partial answer the user already saw.
                logger.info(f"[{session_id}] client disconnected mid-stream")
                raise
            finally:
                tap.finish()
                if query and tap.assistant_text:
                    _spawn(_ingest(handle, query, tap.assistant_text, session_id))
                elif not completed:
                    logger.debug(f"[{session_id}] no reply captured; nothing to persist")
                await client.aclose()

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return router
