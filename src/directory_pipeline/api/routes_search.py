"""Search endpoints.

The query-parameter surface is a deliberate contract, not a passthrough: users
never get to inject raw OpenSearch DSL. That keeps the index free to change
shape (and get reindexed) without breaking callers, and closes off the obvious
denial-of-service vectors that come with arbitrary query bodies.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from opensearchpy import NotFoundError
from pydantic import BaseModel, Field

from ..observability import METRICS, timed
from .deps import Resources, get_resources

router = APIRouter(prefix="/search", tags=["search"])


class SearchResponse(BaseModel):
    total: int
    took_ms: int | None = None
    max_score: float | None = None
    results: list[dict[str, Any]] = Field(default_factory=list)
    facets: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


@router.get("", response_model=SearchResponse)
async def search(
    resources: Annotated[Resources, Depends(get_resources)],
    q: str | None = Query(
        None, description="Free-text query across name, categories, description."
    ),
    city: str | None = None,
    region: Annotated[str | None, Query(max_length=2, description="Two-letter state code.")] = None,
    country: Annotated[str | None, Query(max_length=2)] = None,
    category: Annotated[list[str] | None, Query()] = None,
    industry: str | None = None,
    technology: Annotated[
        list[str] | None, Query(description="Company must have ALL listed.")
    ] = None,
    min_employees: Annotated[int | None, Query(ge=0)] = None,
    max_employees: Annotated[int | None, Query(ge=0)] = None,
    include_duplicates: bool = False,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> SearchResponse:
    try:
        with timed("api.search"):
            payload = await resources.search.search(
                q=q,
                city=city,
                region=region,
                country=country,
                categories=category,
                industry=industry,
                technologies=technology,
                min_employees=min_employees,
                max_employees=max_employees,
                canonical_only=not include_duplicates,
                size=size,
                offset=offset,
            )
    except NotFoundError as exc:
        raise HTTPException(404, "Index alias does not exist. Run the bootstrap first.") from exc
    METRICS.incr("api.search_requests")
    return SearchResponse(**payload)


@router.get("/suggest")
async def suggest(
    resources: Annotated[Resources, Depends(get_resources)],
    prefix: Annotated[str, Query(min_length=2, max_length=64)],
    size: Annotated[int, Query(ge=1, le=25)] = 8,
) -> dict[str, Any]:
    """Type-ahead over the edge-n-gram subfield."""
    return {"suggestions": await resources.search.suggest(prefix, size)}


@router.get("/companies/{record_id}")
async def get_company(
    record_id: str, resources: Annotated[Resources, Depends(get_resources)]
) -> dict[str, Any]:
    document = await resources.search.get(record_id)
    if document is None:
        raise HTTPException(404, f"No company with record_id {record_id}")
    return document


@router.get("/explain/{record_id}")
async def explain(
    record_id: str,
    q: Annotated[str, Query(min_length=1)],
    resources: Annotated[Resources, Depends(get_resources)],
) -> dict[str, Any]:
    """Score breakdown for one document. Relevance debugging, not a user feature."""
    return await resources.search.explain(record_id, q)
