"""Expose the directory over MCP.

This is MCP in the direction that fits the pipeline: handing a finished dataset
to a client, not handing tools to an agent. The pipeline has no agent loop --
see docs/decisions.md -- but the index it builds is exactly the kind of thing
worth querying from a chat client, and the relevance work is already done.

Every tool delegates to SearchClient, so the ranking, the filters and the facet
aggregations are the same ones /search serves. Nothing about query construction
is reimplemented here; a second implementation would drift from the first and
the drift would show up as "MCP gives different answers".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..config import Settings, get_settings
from ..observability import configure_logging, get_logger
from ..search.index import SearchIndex
from ..search.query import SearchClient

log = get_logger(__name__)

# A tool result is spent from the client's context window, so the default shape
# is deliberately narrow. Raw OpenSearch hits carry the full source document,
# the highlight fragments and the index metadata -- tens of kilobytes for a page
# of results, most of it never read. `detail="full"` is there for when it is.
BRIEF_FIELDS = (
    "record_id",
    "name",
    "employee_count",
    "founded_year",
)


def _brief(hit: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: hit.get(k) for k in BRIEF_FIELDS if hit.get(k) is not None}
    address = hit.get("address") or {}
    if city := address.get("city"):
        out["location"] = ", ".join(p for p in (city, address.get("region")) if p)
    if industry := (hit.get("enrichment") or {}).get("industry"):
        out["industry"] = industry
    if categories := hit.get("categories"):
        out["categories"] = categories[:4]
    if website := (hit.get("contact") or {}).get("website"):
        out["website"] = website
    if (score := hit.get("_score")) is not None:
        out["score"] = round(float(score), 3)
    if not hit.get("is_canonical", True):
        # Surfaced rather than hidden: a client asking for duplicates wants to
        # know which record the cluster resolved to.
        out["duplicate_of"] = hit.get("duplicate_of")
    return out


def build_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or get_settings()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        """Own the OpenSearch connections rather than leaking them at exit.

        The clients are still created on first use -- a client that connects on
        startup cannot be launched while the cluster is down, and a chat client
        starting this server should not fail because the index is briefly
        unreachable. Closing them here is the half that was missing.
        """
        try:
            yield state
        finally:
            for key in ("search", "index"):
                held = state.pop(key, None)
                if held is not None:
                    await held.aclose()

    server = MCPServer(
        name="directory-pipeline",
        lifespan=lifespan,
        instructions=(
            "Search a company directory built by a crawl/extract/enrich/resolve "
            "pipeline. Results are ranked by BM25 over company name, categories "
            "and description, with duplicates resolved into canonical records. "
            "Use explain_ranking to see why a specific company scored as it did."
        ),
    )

    def client() -> SearchClient:
        if "search" not in state:
            state["search"] = SearchClient(settings)
        return state["search"]

    @server.tool(
        name="search_companies",
        description=(
            "Search companies by free text and structured filters. Returns ranked "
            "results plus facet counts (city, industry, category, employee band) "
            "computed by the search engine over the whole matching set, not just "
            "the returned page."
        ),
    )
    async def search_companies(
        q: str | None = None,
        city: str | None = None,
        region: str | None = None,
        industry: str | None = None,
        categories: list[str] | None = None,
        technologies: list[str] | None = None,
        min_employees: int | None = None,
        max_employees: int | None = None,
        include_duplicates: bool = False,
        size: int = 10,
        detail: str = "brief",
    ) -> dict[str, Any]:
        """`region` takes a two-letter code. `detail="full"` returns whole documents."""
        try:
            result = await client().search(
                q=q,
                city=city,
                region=region,
                industry=industry,
                categories=categories,
                technologies=technologies,
                min_employees=min_employees,
                max_employees=max_employees,
                canonical_only=not include_duplicates,
                size=min(max(size, 1), 50),
            )
        except Exception as exc:
            return {"error": f"search unavailable: {exc}"}

        results = result.get("results", [])
        return {
            "total": result.get("total"),
            "took_ms": result.get("took_ms"),
            "results": results if detail == "full" else [_brief(r) for r in results],
            "facets": result.get("facets"),
        }

    @server.tool(
        name="get_company",
        description="Fetch one company's full record by its record_id.",
    )
    async def get_company(record_id: str) -> dict[str, Any]:
        try:
            found = await client().get(record_id)
        except Exception as exc:
            return {"error": f"lookup unavailable: {exc}"}
        return found or {"error": f"no company with record_id {record_id!r}"}

    @server.tool(
        name="explain_ranking",
        description=(
            "Explain why a company scored as it did for a query: the BM25 "
            "breakdown, per field, including the boosts the query applies."
        ),
    )
    async def explain_ranking(record_id: str, q: str) -> dict[str, Any]:
        try:
            return await client().explain(record_id, q)
        except Exception as exc:
            return {"error": f"explain unavailable: {exc}"}

    @server.tool(
        name="index_status",
        description=(
            "Whether the index is reachable, which physical index the alias "
            "points at, and how many documents it holds."
        ),
    )
    async def index_status() -> dict[str, Any]:
        index = state.setdefault("index", SearchIndex(settings))
        try:
            if not await index.ping():
                return {"reachable": False, "alias": settings.opensearch_alias}
            return {"reachable": True, **await index.stats()}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    return server


def main() -> None:
    """Entry point for `dp-mcp`.

    stdio is the default because that is how desktop clients launch a server:
    as a subprocess over stdin/stdout. Logging must therefore go nowhere near
    stdout, or it corrupts the protocol stream.
    """
    import os
    import sys

    configure_logging(json_output=True)
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio" and sys.stdout.isatty():
        print(
            "dp-mcp speaks MCP over stdin/stdout; it is meant to be launched by a "
            "client, not run interactively. Set MCP_TRANSPORT=streamable-http to "
            "serve over HTTP instead.",
            file=sys.stderr,
        )
    build_server().run(transport=transport)  # type: ignore[arg-type]
