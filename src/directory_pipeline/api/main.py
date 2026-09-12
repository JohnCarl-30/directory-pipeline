"""FastAPI application.

Health is split into liveness and readiness because they answer different
questions and a load balancer needs both:

  /healthz  -- is the process alive? (never touches dependencies)
  /readyz   -- can it serve traffic? (checks OpenSearch and Temporal)

Conflating them means a brief OpenSearch blip gets every pod restarted.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from ..observability import METRICS
from .deps import Resources, get_resources, lifespan
from .routes_ingest import router as ingest_router
from .routes_search import router as search_router

app = FastAPI(
    title="Directory Pipeline API",
    description=(
        "Agentic crawl -> extract -> enrich -> resolve -> OpenSearch pipeline, "
        "orchestrated by Temporal."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(search_router)
app.include_router(ingest_router)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately dependency-free."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz(resources: Annotated[Resources, Depends(get_resources)]) -> JSONResponse:
    """Readiness. Degraded if OpenSearch is down; Temporal is search-optional."""
    opensearch_ok = await resources.index.ping()
    temporal_ok = resources.temporal is not None
    ready = opensearch_ok
    return JSONResponse(
        {
            "ready": ready,
            "opensearch": "ok" if opensearch_ok else "unreachable",
            "temporal": "ok" if temporal_ok else "unreachable",
        },
        status_code=200 if ready else 503,
    )


@app.get("/metrics", tags=["ops"])
async def metrics() -> dict[str, Any]:
    """In-process counters and latency percentiles.

    Swap for a Prometheus exporter when you have a collector; the shape is the
    same and the call sites do not change.
    """
    return METRICS.snapshot()


@app.get("/", tags=["ops"])
async def root() -> dict[str, Any]:
    return {
        "service": "directory-pipeline",
        "docs": "/docs",
        "endpoints": {
            "search": "/search?q=analytics&region=TX",
            "suggest": "/search/suggest?prefix=nor",
            "start_crawl": "POST /ingest/crawl",
            "reindex": "POST /ingest/reindex",
            "index_stats": "/ingest/index/stats",
            "metrics": "/metrics",
        },
    }


def run() -> None:
    import uvicorn

    uvicorn.run("directory_pipeline.api.main:app", host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    run()
