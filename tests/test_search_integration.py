"""Integration tests against a real OpenSearch.

Skipped automatically when no cluster is reachable, so the suite stays runnable
with no Docker. Run them with:

    docker compose up -d opensearch && pytest tests/test_search_integration.py

These cover the things a unit test structurally cannot: that the mappings are
actually accepted, that the analyzers tokenize the way the query assumes, and
that the alias swap is atomic and reversible.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from directory_pipeline.config import Settings
from directory_pipeline.domain.models import (
    Address,
    CompanyRecord,
    Contact,
    EnrichedCompany,
    Enrichment,
)
from directory_pipeline.search.index import SearchIndex
from directory_pipeline.search.query import SearchClient

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")


def _reachable() -> bool:
    try:
        return httpx.get(f"{OPENSEARCH_URL}/_cluster/health", timeout=2).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no OpenSearch at {OPENSEARCH_URL}")


def company(
    name: str,
    *,
    sid: str,
    city: str,
    region: str,
    website: str,
    industry: str = "Software",
    employees: int = 100,
    technologies: list[str] | None = None,
) -> EnrichedCompany:
    from directory_pipeline.extraction.normalize import normalize_company_name

    return EnrichedCompany(
        company=CompanyRecord(
            record_id=CompanyRecord.make_record_id("itest", sid),
            source="itest",
            source_id=sid,
            source_url=f"http://itest/{sid}",
            name=name,
            name_normalized=normalize_company_name(name),
            categories=["Analytics", "Data Platform"],
            description=f"{name} builds data infrastructure for retailers.",
            address=Address(city=city, region=region, postal_code="78701"),
            contact=Contact(website=website, phone_e164="+15125550142"),
            employee_count=employees,
        ),
        enrichment=Enrichment(
            provider="itest",
            industry=industry,
            employee_count=employees,
            technologies=technologies or ["Kafka", "Snowflake"],
            confidence=0.9,
        ),
    )


@pytest.fixture
async def index():
    """A throwaway alias per test, torn down afterwards."""
    alias = f"itest-companies-{uuid.uuid4().hex[:8]}"
    settings = Settings(opensearch_url=OPENSEARCH_URL, opensearch_alias=alias)
    idx = SearchIndex(settings)
    await idx.bootstrap(alias)
    try:
        yield idx, settings, alias
    finally:
        try:
            await idx.client.indices.delete(index=f"{alias}-*", ignore_unavailable=True)
        finally:
            await idx.aclose()


async def _seed(idx: SearchIndex, alias: str, docs: list[EnrichedCompany]) -> None:
    await idx.index_documents(docs, alias=alias)
    await idx.client.indices.refresh(index=alias)


async def test_strict_mapping_accepts_a_real_document(index):
    """`dynamic: strict` means a document shape mismatch is a hard 400."""
    idx, _, alias = index
    count = await idx.index_documents(
        [
            company(
                "Northwind Analytics, Inc.",
                sid="1",
                city="Austin",
                region="TX",
                website="https://northwind.com",
            )
        ],
        alias=alias,
    )
    assert count == 1


async def test_ingest_pipeline_stamps_indexed_at(index):
    idx, settings, alias = index
    await _seed(
        idx,
        alias,
        [
            company(
                "Atlas Robotics", sid="2", city="Seattle", region="WA", website="https://atlas.ai"
            )
        ],
    )
    hit = await SearchClient(settings, client=idx.client).get(
        CompanyRecord.make_record_id("itest", "2")
    )
    assert hit is not None
    assert hit.get("indexed_at"), "ingest pipeline should stamp a server-side write time"


async def test_legal_suffix_is_ignored_at_search_time(index):
    """The analyzer must make 'Acme Inc' findable as 'Acme' and vice versa."""
    idx, settings, alias = index
    await _seed(
        idx,
        alias,
        [
            company(
                "Northwind Analytics, Inc.",
                sid="1",
                city="Austin",
                region="TX",
                website="https://northwind.com",
            )
        ],
    )
    search = SearchClient(settings, client=idx.client)

    for query in ("Northwind Analytics", "northwind analytics llc", "Northwind"):
        result = await search.search(q=query)
        assert result["total"] >= 1, f"{query!r} should match"


async def test_edge_ngram_powers_typeahead(index):
    idx, settings, alias = index
    await _seed(
        idx,
        alias,
        [
            company(
                "Northwind Analytics, Inc.",
                sid="1",
                city="Austin",
                region="TX",
                website="https://northwind.com",
            )
        ],
    )
    suggestions = await SearchClient(settings, client=idx.client).suggest("nor")
    assert any("Northwind" in s["name"] for s in suggestions)


async def test_filters_and_facets(index):
    idx, settings, alias = index
    await _seed(
        idx,
        alias,
        [
            company(
                "Austin Co",
                sid="a",
                city="Austin",
                region="TX",
                website="https://austin.com",
                employees=30,
            ),
            company(
                "Seattle Co",
                sid="b",
                city="Seattle",
                region="WA",
                website="https://seattle.com",
                employees=900,
                technologies=["Go", "Kafka"],
            ),
        ],
    )
    search = SearchClient(settings, client=idx.client)

    assert (await search.search(q=None, region="TX"))["total"] == 1
    assert (await search.search(q=None, min_employees=500))["total"] == 1

    # Technologies are ANDed: Kafka AND Go excludes the Kafka-only company.
    both = await search.search(q=None, technologies=["Kafka", "Go"])
    assert both["total"] == 1

    facets = (await search.search(q=None))["facets"]
    assert {f["key"] for f in facets["by_city"]} == {"Austin", "Seattle"}


async def test_duplicates_are_hidden_but_retrievable(index):
    """Duplicates stay indexed for provenance; search hides them by default."""
    idx, settings, alias = index
    canonical = company(
        "Atlas Robotics", sid="a", city="Seattle", region="WA", website="https://atlas.ai"
    )
    duplicate = company(
        "Atlas Robotics, Inc.", sid="b", city="Seattle", region="WA", website="https://atlas.ai"
    )
    duplicate = duplicate.model_copy(update={"duplicate_of": canonical.company.record_id})
    await _seed(idx, alias, [canonical, duplicate])
    search = SearchClient(settings, client=idx.client)

    assert (await search.search(q="Atlas Robotics"))["total"] == 1
    assert (await search.search(q="Atlas Robotics", canonical_only=False))["total"] == 2
    # The hidden record is still there when asked for directly.
    assert await search.get(duplicate.company.record_id) is not None


async def test_reindex_swaps_the_alias_atomically_and_preserves_documents(index):
    """The headline claim: zero-downtime reindex behind an alias."""
    idx, settings, alias = index
    await _seed(
        idx,
        alias,
        [
            company(
                f"Company {i}", sid=str(i), city="Austin", region="TX", website=f"https://c{i}.com"
            )
            for i in range(25)
        ],
    )

    before_index = await idx.resolve_alias(alias)
    before_count = (await idx.stats(alias))["documents"]
    assert before_count == 25

    result = await idx.reindex(alias=alias)

    assert result["swapped"] is True
    assert result["documents_copied"] == 25
    assert result["target_index"] != before_index
    assert result["old_index_dropped"] is False, "old index is the rollback path"

    # The alias now points somewhere new, and serves the same documents.
    assert await idx.resolve_alias(alias) == result["target_index"]
    await idx.client.indices.refresh(index=alias)
    assert (await idx.stats(alias))["documents"] == 25

    # Exactly one index behind the alias at all times -- never zero, never two.
    aliased = await idx.client.indices.get_alias(name=alias)
    assert len(aliased) == 1

    # Searches keep working through the swap with no client-side change.
    assert (await SearchClient(settings, client=idx.client).search(q="Company"))["total"] == 25


async def test_rollback_restores_the_previous_index(index):
    idx, settings, alias = index
    await _seed(
        idx,
        alias,
        [company("Rollback Co", sid="1", city="Austin", region="TX", website="https://r.com")],
    )
    original = await idx.resolve_alias(alias)

    result = await idx.reindex(alias=alias)
    assert await idx.resolve_alias(alias) == result["target_index"]

    await idx.rollback(alias, original)
    assert await idx.resolve_alias(alias) == original
    await idx.client.indices.refresh(index=alias)
    assert (await idx.stats(alias))["documents"] == 1


async def test_reindex_from_a_missing_alias_bootstraps_instead_of_failing(index):
    idx, _, _ = index
    fresh = f"itest-empty-{uuid.uuid4().hex[:8]}"
    try:
        result = await idx.reindex(alias=fresh)
        assert result["source_index"] is None
        assert result["documents_copied"] == 0
        assert await idx.resolve_alias(fresh) == result["target_index"]
    finally:
        await idx.client.indices.delete(index=f"{fresh}-*", ignore_unavailable=True)
