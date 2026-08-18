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
                 context_ttl: int = 3600):
        self.client = HMLRClient(api_key=api_key, db_path=db_path)
        self.contexts = ContextStore(ttl_seconds=context_ttl)
        logger.info(f"Memory service ready (db={self.client.db_path})")


def create_app(db_path: Optional[str] = None,
               api_key: Optional[str] = None,
               context_ttl: int = 3600,
               upstream_url: Optional[str] = None,
               upstream_key: Optional[str] = None,
               bridge_url: Optional[str] = None) -> FastAPI:

    service: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Built here rather than at import time so that loading this module
        # never triggers model downloads or database creation.
        service["instance"] = MemoryService(
            db_path=db_path or os.getenv("HMLR_DB_PATH"),
            api_key=api_key,
            context_ttl=context_ttl,
        )
        yield
        # Streamed turns are persisted after the response is delivered, so a
        # write can still be in flight when shutdown begins.
        from .anthropic_handler import flush_pending_writes
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
        return HealthResponse(
            status="ok",
            version=VERSION,
            db_path=str(instance.client.db_path),
            sessions_cached=len(instance.contexts),
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

    # Anthropic-compatible proxy. Mounted only when an upstream is configured,
    # so the service can also run as memory-only with no LLM credentials.
    resolved_upstream = upstream_url or os.getenv("HMLR_UPSTREAM_URL")
    if resolved_upstream:
        from .anthropic_handler import create_router

        app.include_router(create_router(
            get_service=get_service,
            upstream_url=resolved_upstream,
            upstream_key=upstream_key or os.getenv("ANTHROPIC_API_KEY"),
            bridge_url=bridge_url,
        ))
        logger.info(f"Anthropic proxy enabled -> {resolved_upstream}")

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
                        help="Anthropic base URL; enables the proxy endpoint. "
                             "e.g. https://api.anthropic.com")
    parser.add_argument("--upstream-key", default=os.getenv("ANTHROPIC_API_KEY"),
                        help="Key sent upstream. Omit to pass the client's own through.")
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
                   bridge_url=bridge),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
