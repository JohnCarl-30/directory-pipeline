"""Query construction and relevance tuning.

Two ideas do most of the work here.

**Filter vs. query context.** Anything binary -- city equals Austin, is_canonical
is true -- goes in `filter`, not `must`. Filters do not contribute to the score
and are cached by OpenSearch as bitsets. Putting a term filter in `must` both
pollutes relevance with meaningless score contributions and forfeits the cache.

**Multi-field matching with deliberate weights.** A hit on the company name is
worth far more than a hit in a description paragraph. `multi_match` with
per-field boosts expresses that; `best_fields` means a document is scored by its
single strongest field rather than by summing weak matches across many.

`function_score` then applies a gentle completeness multiplier so that between
two equally relevant companies, the one with a phone number and a website ranks
first. Kept gentle on purpose -- relevance boosting that overwhelms the text
score produces results that are popular rather than correct.
"""

from __future__ import annotations

from typing import Any

from opensearchpy import AsyncOpenSearch

from ..config import Settings

# Boosts are relative, not absolute. Ratios are what matter.
FIELD_BOOSTS = [
    "name^6",
    "name.ngram^2",
    "legal_name^3",
    "categories^2",
    "enrichment.industry.text^1.5",
    "description^1",
    "address.full^0.5",
]


def build_query(
    q: str | None = None,
    *,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
    categories: list[str] | None = None,
    industry: str | None = None,
    technologies: list[str] | None = None,
    min_employees: int | None = None,
    max_employees: int | None = None,
    canonical_only: bool = True,
    size: int = 20,
    offset: int = 0,
    sort_by_relevance: bool = True,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    if canonical_only:
        # Duplicates stay indexed for provenance but are hidden from search.
        filters.append({"term": {"is_canonical": True}})
    if city:
        filters.append({"term": {"address.city": city}})
    if region:
        filters.append({"term": {"address.region": region.upper()}})
    if country:
        filters.append({"term": {"address.country": country.upper()}})
    if categories:
        filters.append({"terms": {"categories.keyword": categories}})
    if industry:
        filters.append({"term": {"enrichment.industry": industry}})
    if technologies:
        # `must` semantics: a company must have ALL requested technologies.
        filters.extend({"term": {"enrichment.technologies": t}} for t in technologies)
    if min_employees is not None or max_employees is not None:
        bounds: dict[str, int] = {}
        if min_employees is not None:
            bounds["gte"] = min_employees
        if max_employees is not None:
            bounds["lte"] = max_employees
        filters.append({"range": {"employee_count": bounds}})

    if q:
        text_query: dict[str, Any] = {
            "multi_match": {
                "query": q,
                "fields": FIELD_BOOSTS,
                "type": "best_fields",
                "tie_breaker": 0.3,  # partial credit for other matching fields
                "fuzziness": "AUTO",  # tolerate one typo in a company name
                "prefix_length": 2,  # ...but not in the first two characters
            }
        }
    else:
        text_query = {"match_all": {}}

    inner: dict[str, Any] = {"bool": {"must": [text_query], "filter": filters}}

    scored: dict[str, Any] = {
        "function_score": {
            "query": inner,
            "functions": [
                {
                    # Richer records rank higher, but only by up to ~30%.
                    "field_value_factor": {
                        "field": "completeness",
                        "factor": 1.3,
                        "modifier": "sqrt",
                        "missing": 0.5,
                    }
                },
                {
                    # Slight penalty for records the model had to guess at.
                    "filter": {"term": {"extraction.method": "llm"}},
                    "weight": 0.95,
                },
            ],
            "score_mode": "multiply",
            "boost_mode": "multiply",
        }
    }

    body: dict[str, Any] = {
        "query": scored if q else inner,
        "from": offset,
        "size": size,
        "track_total_hits": True,
        "_source": {"excludes": ["address.line1"]},
        "highlight": {
            "fields": {"description": {"fragment_size": 160, "number_of_fragments": 1}},
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
        },
        "aggs": {
            "by_city": {"terms": {"field": "address.city", "size": 10}},
            "by_industry": {"terms": {"field": "enrichment.industry", "size": 10}},
            "by_category": {"terms": {"field": "categories.keyword", "size": 10}},
            "employee_bands": {
                "range": {
                    "field": "employee_count",
                    "ranges": [
                        {"key": "1-10", "to": 11},
                        {"key": "11-50", "from": 11, "to": 51},
                        {"key": "51-200", "from": 51, "to": 201},
                        {"key": "201-1000", "from": 201, "to": 1001},
                        {"key": "1000+", "from": 1001},
                    ],
                }
            },
        },
    }
    if not sort_by_relevance:
        body["sort"] = [{"completeness": "desc"}, {"record_id": "asc"}]
    return body


def build_suggest_query(prefix: str, size: int = 8) -> dict[str, Any]:
    """Type-ahead against the edge-n-gram subfield."""
    return {
        "query": {
            "bool": {
                "must": [{"match": {"name.ngram": {"query": prefix, "operator": "and"}}}],
                "filter": [{"term": {"is_canonical": True}}],
            }
        },
        "size": size,
        "_source": ["record_id", "name", "address.city", "address.region"],
    }


class SearchClient:
    def __init__(self, settings: Settings, client: AsyncOpenSearch | None = None) -> None:
        self.settings = settings
        self.alias = settings.opensearch_alias
        self.client = client or AsyncOpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=settings.opensearch_auth,
            verify_certs=False,
            ssl_show_warn=False,
            timeout=15,
        )

    async def aclose(self) -> None:
        await self.client.close()

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        body = build_query(**kwargs)
        response = await self.client.search(index=self.alias, body=body)
        return _shape(response)

    async def suggest(self, prefix: str, size: int = 8) -> list[dict[str, Any]]:
        response = await self.client.search(
            index=self.alias, body=build_suggest_query(prefix, size)
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def get(self, record_id: str) -> dict[str, Any] | None:
        response = await self.client.search(
            index=self.alias,
            body={"query": {"term": {"record_id": record_id}}, "size": 1},
        )
        hits = response["hits"]["hits"]
        return hits[0]["_source"] if hits else None

    async def explain(self, record_id: str, q: str) -> dict[str, Any]:
        """Why did this document score what it scored? Relevance-tuning tool."""
        index = self.alias
        hit = await self.client.search(
            index=index, body={"query": {"term": {"record_id": record_id}}, "size": 1}
        )
        docs = hit["hits"]["hits"]
        if not docs:
            return {"error": "not found"}
        return await self.client.explain(
            index=docs[0]["_index"],
            id=docs[0]["_id"],
            body={"query": build_query(q=q)["query"]},
        )


def _shape(response: dict[str, Any]) -> dict[str, Any]:
    hits = response["hits"]
    return {
        "total": hits["total"]["value"],
        "max_score": hits.get("max_score"),
        "took_ms": response.get("took"),
        "results": [
            {
                **hit["_source"],
                "_score": hit["_score"],
                "_highlight": hit.get("highlight", {}),
            }
            for hit in hits["hits"]
        ],
        "facets": {
            key: [{"key": b.get("key"), "count": b["doc_count"]} for b in value.get("buckets", [])]
            for key, value in (response.get("aggregations") or {}).items()
        },
    }
