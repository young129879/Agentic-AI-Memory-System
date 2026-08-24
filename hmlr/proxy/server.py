"""
HTTP entry point for HMLR memory.

Three endpoints:
    GET  /health          liveness
    POST /memory/recall   retrieve memory for a turn (no generation)
    POST /memory/ingest   persist a turn generated elsewhere

Run with:
    python -m hmlr.proxy.server
    uvicorn hmlr.proxy.server:app --port 8100
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from hmlr.client import HMLRClient

from .context_store import ContextStore
from .schemas import (
    HealthResponse,
    IngestRequest,
    IngestResponse,
    RecallRequest,
    RecallResponse,
)

logger = logging.getLogger(__name__)

VERSION = "0.1.0"


class MemoryService:
    """Holds the one HMLRClient and the in-flight context store."""

    def __init__(self, db_path: Optional[str] = None,
                 api_key: Optional[str] = None,
                 context_ttl: int = 3600,
                 injection_cache=None,
                 auto_gardener=None):
        self.client = HMLRClient(api_key=api_key, db_path=db_path)
        self.contexts = ContextStore(ttl_seconds=context_ttl)
        self.injection = injection_cache
        self.auto_gardener = auto_gardener
        logger.info(f"Memory service ready (db={self.client.db_path})")

    def start_background(self) -> None:
        """Start non-blocking background workers (automatic gardening)."""
        if self.auto_gardener is not None:
            self.auto_gardener.start()

    async def stop_background(self) -> None:
        """Stop background workers gracefully."""
        if self.auto_gardener is not None:
            await self.auto_gardener.stop()


def _make_gardener(service: "MemoryService", inactive_days: int,
                   interval_hours: float):
    """
    Build an AutoGardener over the client's components, or None when the
    necessary pieces are unavailable (e.g. no LLM client configured).

    A failure here must not block service startup; it only disables automatic
    gardening for this process.
    """
    components = getattr(service.client, "components", None)
    if components is None:
        logger.warning("AutoGardener disabled: client has no components")
        return None
    try:
        gardener = components.gardener
        if gardener is None:
            logger.warning("AutoGardener disabled: no gardener component")
            return None
        from .auto_gardener import AutoGardener
        return AutoGardener(
            components.storage,
            gardener,
            interval_hours=interval_hours,
            inactive_days=inactive_days,
            enabled=True,
        )
    except Exception as e:
        logger.error(f"AutoGardener init failed: {e}", exc_info=True)
        return None


def create_app(db_path: Optional[str] = None,
               api_key: Optional[str] = None,
               context_ttl: int = 3600,
               upstream_url: Optional[str] = None,
               upstream_key: Optional[str] = None,
               openai_upstream_url: Optional[str] = None,
               openai_upstream_key: Optional[str] = None,
               bridge_url: Optional[str] = None,
               injection_ttl: int = 7200,
               gardener_inactive_days: Optional[int] = None,
               gardener_interval_hours: Optional[float] = None) -> FastAPI:

    service: dict = {}

    # Owned here rather than by MemoryService because the proxy router is
    # built before startup and needs the same instance.
    from .injection_cache import InjectionCache
    injection_cache = InjectionCache(ttl_seconds=injection_ttl)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Built here rather than at import time so that loading this module
        # never triggers model downloads or database creation.
        inactive = int(os.getenv("HMLR_GARDENER_INACTIVE_DAYS", 30)) \
            if gardener_inactive_days is None else int(gardener_inactive_days)
        interval = float(os.getenv("HMLR_GARDENER_INTERVAL_HOURS", 24)) \
            if gardener_interval_hours is None else float(gardener_interval_hours)

        instance = MemoryService(
            db_path=db_path or os.getenv("HMLR_DB_PATH"),
            api_key=api_key,
            context_ttl=context_ttl,
            injection_cache=injection_cache,
        )
        if os.getenv("HMLR_GARDENER_ENABLED", "1").lower() not in ("0", "false", "no"):
            instance.auto_gardener = _make_gardener(instance, inactive, interval)
        instance.start_background()
        service["instance"] = instance
        yield
        # Streamed turns are persisted after the response is delivered, so a
        # write can still be in flight when shutdown begins.
        await instance.stop_background()
        from .llm_handler import flush_pending_writes
        flushed = await flush_pending_writes(timeout=10.0)
        if flushed:
            logger.info(f"Flushed {flushed} pending memory writes")
        service.clear()

    app = FastAPI(
        title="HMLR Memory Service",
        version=VERSION,
        description="Split-phase memory for agents that bring their own model.",
        lifespan=lifespan,
    )

    def get_service() -> MemoryService:
        instance = service.get("instance")
        if instance is None:
            raise HTTPException(status_code=503, detail="Service not ready")
        return instance

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        instance = service.get("instance")
        if instance is None:
            return HealthResponse(status="starting", version=VERSION)
        gardener = None
        if instance.auto_gardener is not None:
            gardener = {
                "enabled": instance.auto_gardener.enabled,
                "inactive_days": instance.auto_gardener.inactive_days,
                "interval_hours": instance.auto_gardener.interval_hours,
                "sweeps": instance.auto_gardener.sweep_count,
                "last": instance.auto_gardener.last_sweep_result,
            }
        return HealthResponse(
            status="ok",
            version=VERSION,
            db_path=str(instance.client.db_path),
            sessions_cached=len(instance.contexts),
            injection_cache=injection_cache.stats(),
            auto_gardener=gardener,
        )

    @app.post("/memory/recall", response_model=RecallResponse)
    async def recall(req: RecallRequest) -> RecallResponse:
        """
        Retrieve memory for a turn.

        Routing is a write -- it opens or resumes a bridge block -- so this
        must be called once per turn, before generation, not speculatively.
        """
        instance = get_service()
        result = await instance.client.recall(req.query, session_id=req.session_id)

        # Stored even when degraded: the turn still has a turn_id and possibly
        # a block, and ingest needs them to file the reply correctly.
        handle = instance.contexts.put(result)

        return RecallResponse(
            session_id=result.session_id,
            turn_id=result.turn_id,
            block_id=result.block_id,
            is_new_topic=result.is_new_topic,
            dossiers=result.dossiers,
            facts=result.facts,
            block_facts=result.block_facts,
            open_loops=result.open_loops,
            block_index=result.block_index,
            memory_count=len(result.memories),
            context=handle,
            degraded=result.degraded,
            error=result.error,
        )

    @app.post("/memory/ingest", response_model=IngestResponse)
    async def ingest(req: IngestRequest) -> IngestResponse:
        """
        Persist a completed turn.

        A missing or expired handle is not an error: the turn is still logged,
        just not attached to its block. Losing the raw turn would be worse.
        """
        instance = get_service()
        ctx = instance.contexts.take(req.context)

        ok = await instance.client.ingest(
            req.user_message,
            req.assistant_reply,
            ctx=ctx,
            session_id=req.session_id,
        )

        if ok:
            return IngestResponse(ok=True)
        if ctx is None:
            return IngestResponse(
                ok=False, detail="No valid context handle; turn logged only"
            )
        return IngestResponse(ok=False, detail="Failed to attach turn to block")

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        # Memory failing must not take the caller's conversation down with it.
        logger.error(f"Unhandled error on {request.url.path}: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal memory service error", "error": str(exc)},
        )

    # Read-only access to memory the model was told about but not given in
    # full. Injection advertises these, so they mount unconditionally.
    from .bridge import create_router as create_bridge_router

    app.include_router(create_bridge_router(get_service))

    # LLM proxies. Mounted only when an upstream is configured, so the
    # service can also run as memory-only with no LLM credentials.
    #
    # Both protocols may run at once against different upstreams: an agent
    # speaking Anthropic and one speaking OpenAI can share a memory store.
    from .llm_handler import ANTHROPIC, OPENAI, create_router

    anthropic_upstream = upstream_url or os.getenv("HMLR_UPSTREAM_URL")
    if anthropic_upstream:
        app.include_router(create_router(
            get_service=get_service,
            upstream_url=anthropic_upstream,
            upstream_key=upstream_key or os.getenv("ANTHROPIC_API_KEY"),
            bridge_url=bridge_url,
            injection_cache=injection_cache,
            protocol=ANTHROPIC,
        ))
        logger.info(f"Anthropic proxy enabled -> {anthropic_upstream}")

    openai_upstream = openai_upstream_url or os.getenv("HMLR_OPENAI_UPSTREAM_URL")
    if openai_upstream:
        app.include_router(create_router(
            get_service=get_service,
            upstream_url=openai_upstream,
            upstream_key=openai_upstream_key or os.getenv("OPENAI_API_KEY"),
            bridge_url=bridge_url,
            injection_cache=injection_cache,
            protocol=OPENAI,
        ))
        logger.info(f"OpenAI proxy enabled -> {openai_upstream}")

    return app


app = create_app()


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the HMLR memory service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--db", default=os.getenv("HMLR_DB_PATH"),
                        help="SQLite path (default: HMLR's own default)")
    parser.add_argument("--upstream",
                        default=os.getenv("HMLR_UPSTREAM_URL"),
                        help="Anthropic base URL; enables POST /v1/messages. "
                             "e.g. https://api.anthropic.com")
    parser.add_argument("--upstream-key", default=os.getenv("ANTHROPIC_API_KEY"),
                        help="Key sent upstream. Omit to pass the client's own through.")
    parser.add_argument("--openai-upstream",
                        default=os.getenv("HMLR_OPENAI_UPSTREAM_URL"),
                        help="OpenAI-compatible base URL; enables "
                             "POST /v1/chat/completions. e.g. https://api.openai.com")
    parser.add_argument("--openai-upstream-key", default=os.getenv("OPENAI_API_KEY"),
                        help="Key sent to the OpenAI-compatible upstream.")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bridge = f"http://{args.host}:{args.port}/memory"

    uvicorn.run(
        create_app(db_path=args.db,
                   upstream_url=args.upstream,
                   upstream_key=args.upstream_key,
                   openai_upstream_url=args.openai_upstream,
                   openai_upstream_key=args.openai_upstream_key,
                   bridge_url=bridge),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
