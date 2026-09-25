"""FastAPI application.

Health is split into liveness and readiness because they answer different
questions and a load balancer needs both:

  /healthz  -- is the process alive? (never touches dependencies)
  /readyz   -- can it serve traffic? (checks OpenSearch and Temporal)

Conflating them means a brief OpenSearch blip gets every pod restarted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse

from ..config import get_settings
from ..observability import METRICS, summarize
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

_UI = Path(__file__).parent / "static" / "index.html"

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


@app.get("/metrics/summary", tags=["ops"])
async def metrics_summary() -> dict[str, Any]:
    """The same counters, with the arithmetic already done.

    /metrics answers "how many". This answers the questions someone actually
    asks of a running pipeline: how fast is it going, how often did it need the
    model, is prompt caching still working, and what is it costing. Cost appears
    only once token rates are configured.
    """
    return summarize(METRICS.snapshot(), cost_per_mtok=get_settings().llm_cost_rates)


@app.get("/ui", tags=["ops"], include_in_schema=False)
async def ui() -> FileResponse:
    """The search console.

    One self-contained HTML file rather than a bundled frontend: this is a
    Python service, and a build step plus a node_modules tree would cost more
    than the page is worth. It talks to the same public endpoints any other
    client would.
    """
    return FileResponse(_UI, media_type="text/html")


@app.get("/", tags=["ops"])
async def root() -> dict[str, Any]:
    return {
        "service": "directory-pipeline",
        "ui": "/ui",
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
