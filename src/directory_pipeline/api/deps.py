"""Shared resources with a lifespan-managed lifecycle.

Clients are created once at startup and closed at shutdown. Creating an
OpenSearch or Temporal client per request is the single most common way a
Python service ends up with connection exhaustion under load.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request

from ..config import Settings, get_settings
from ..observability import configure_logging, get_logger
from ..search.index import SearchIndex
from ..search.query import SearchClient

if TYPE_CHECKING:  # keep temporalio out of the import path for search-only use
    from temporalio.client import Client

log = get_logger(__name__)


@dataclass
class Resources:
    settings: Settings
    search: SearchClient
    index: SearchIndex
    temporal: Client | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()

    resources = Resources(
        settings=settings,
        search=SearchClient(settings),
        index=SearchIndex(settings),
    )

    # Temporal is optional: search endpoints must keep serving even when the
    # orchestration layer is down. Only ingest endpoints need it.
    try:
        from ..orchestration.worker import connect

        resources.temporal = await connect(settings)
        log.info("api.temporal_connected", address=settings.temporal_address)
    except Exception as exc:
        log.warning("api.temporal_unavailable", error=str(exc))

    app.state.resources = resources
    log.info("api.started", opensearch=settings.opensearch_url)
    try:
        yield
    finally:
        await resources.search.aclose()
        await resources.index.aclose()
        log.info("api.stopped")


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


def require_temporal(request: Request) -> Client:
    resources = get_resources(request)
    if resources.temporal is None:
        raise HTTPException(
            status_code=503,
            detail="Temporal is not reachable; ingest endpoints are unavailable.",
        )
    return resources.temporal
