"""Incremental re-crawl: skip a page whose body has not changed.

The machinery for this was already in the repo and wired to nothing --
`seen_hashes` threaded from workflow to activity to crawler, `content_hash`
computed on every listing, `CrawlRequest.force_refetch` declared and read by
no one. Every crawl therefore re-fetched and re-extracted everything, which is
exactly where the model cost sits.

These cover the two halves that were missing: reading the last known hash back
out of the index, and honouring the override.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from opensearchpy import NotFoundError

from directory_pipeline.config import Settings
from directory_pipeline.domain.models import (
    Address,
    CompanyRecord,
    Contact,
    EnrichedCompany,
)
from directory_pipeline.scraping.crawler import DirectoryCrawler, source_id_from_url
from directory_pipeline.search.index import _MGET_CHUNK, SearchIndex


def fast_settings(**overrides: Any) -> Settings:
    return Settings(
        crawl_rps=1000.0,
        crawl_burst=1000,
        request_timeout_s=2.0,
        obey_robots=False,
        **overrides,
    )


class FakeClient:
    """Records mget calls and replays canned responses."""

    def __init__(self, docs: dict[str, str] | None = None, *, raise_not_found: bool = False):
        self.docs = docs or {}
        self.raise_not_found = raise_not_found
        self.calls: list[list[str]] = []

    async def mget(self, *, body: dict[str, Any], index: str, _source: Any) -> dict[str, Any]:
        if self.raise_not_found:
            raise NotFoundError(404, "index_not_found_exception", {})
        ids = body["ids"]
        self.calls.append(list(ids))
        return {
            "docs": [
                (
                    {"_id": i, "found": True, "_source": {"content_hash": self.docs[i]}}
                    if i in self.docs
                    else {"_id": i, "found": False}
                )
                for i in ids
            ]
        }


def index_with(docs: dict[str, str] | None = None, **kwargs: Any) -> tuple[SearchIndex, FakeClient]:
    client = FakeClient(docs, **kwargs)
    return SearchIndex(fast_settings(), client=client), client  # type: ignore[arg-type]


def rid(source: str, source_id: str) -> str:
    return CompanyRecord.make_record_id(source, source_id)


# --------------------------------------------------------------------------
# Reading hashes back out of the index
# --------------------------------------------------------------------------


async def test_hashes_come_back_keyed_by_source_id_not_record_id():
    """The crawler compares against source_id, so that is what must come out.

    Returning record_ids would be a map whose keys never match the ones
    `fetch_details` looks up, and the result is not an error -- it is a crawl
    that silently never skips anything.
    """
    idx, _ = index_with({rid("demo", "acme"): "abc123"})
    assert await idx.content_hashes("demo", ["acme"]) == {"acme": "abc123"}


async def test_documents_that_do_not_exist_are_simply_absent():
    idx, _ = index_with({rid("demo", "acme"): "abc123"})
    assert await idx.content_hashes("demo", ["acme", "never-seen"]) == {"acme": "abc123"}


async def test_an_empty_hash_counts_as_no_hash():
    """Records written before the field existed carry "". Refetch them."""
    idx, _ = index_with({rid("demo", "acme"): "", rid("demo", "beta"): "def456"})
    assert await idx.content_hashes("demo", ["acme", "beta"]) == {"beta": "def456"}


async def test_no_source_ids_asks_the_cluster_nothing():
    idx, client = index_with()
    assert await idx.content_hashes("demo", []) == {}
    assert client.calls == []


async def test_a_missing_alias_is_a_first_run_not_a_failure():
    """Nothing indexed yet means no history, which is not an error."""
    idx, _ = index_with(raise_not_found=True)
    assert await idx.content_hashes("demo", ["acme"]) == {}


async def test_the_same_source_id_under_a_different_source_is_a_different_record():
    """record_id is sha1(source:source_id) -- the namespacing must be honoured."""
    idx, _ = index_with({rid("demo", "acme"): "abc123"})
    assert await idx.content_hashes("other-directory", ["acme"]) == {}


async def test_large_lookups_are_chunked():
    ids = [f"c{n}" for n in range(_MGET_CHUNK * 2 + 5)]
    idx, client = index_with({rid("demo", i): f"h-{i}" for i in ids})

    hashes = await idx.content_hashes("demo", ids)

    assert len(hashes) == len(ids)
    assert len(client.calls) == 3, "one request per chunk"
    assert [len(c) for c in client.calls] == [_MGET_CHUNK, _MGET_CHUNK, 5]


# --------------------------------------------------------------------------
# The hash has to reach the document, or there is nothing to read back
# --------------------------------------------------------------------------


def test_the_indexed_document_carries_the_content_hash():
    """`dynamic: strict` means this field has to be mapped *and* written.

    The record carried a content_hash all along and `to_document` dropped it,
    so there was nothing in the index to compare a re-crawl against.
    """
    record = CompanyRecord(
        record_id=rid("demo", "acme"),
        source="demo",
        source_id="acme",
        source_url="http://demo/acme",
        name="Acme",
        name_normalized="acme",
        address=Address(city="Austin", region="TX"),
        contact=Contact(),
        content_hash="deadbeef",
    )
    doc = EnrichedCompany(company=record).to_document()
    assert doc["content_hash"] == "deadbeef"


def test_the_mapping_declares_content_hash():
    from directory_pipeline.search.index import MAPPINGS

    field = MAPPINGS["properties"]["content_hash"]
    assert field["type"] == "keyword"
    assert field["index"] is False, "only ever read by id; indexing it buys nothing"


# --------------------------------------------------------------------------
# The crawler's half: a matching hash means the page is not yielded
# --------------------------------------------------------------------------


@respx.mock
async def test_an_unchanged_page_is_dropped_before_extraction():
    body = "<html><body><h1 class='company-name'>Acme</h1></body></html>"
    route = respx.get("http://d.test/company/acme").mock(
        return_value=httpx.Response(200, text=body)
    )
    crawler = DirectoryCrawler(fast_settings())

    first = [
        listing async for listing in crawler.fetch_details(["http://d.test/company/acme"], "demo")
    ]
    assert len(first) == 1

    known = {first[0].source_id: first[0].content_hash}
    second = [
        listing
        async for listing in crawler.fetch_details(
            ["http://d.test/company/acme"], "demo", seen_hashes=known
        )
    ]

    assert second == [], "unchanged page should not reach extraction"
    assert route.call_count == 2, "the page is still fetched -- the hash is of its body"
    await crawler.aclose()


@respx.mock
async def test_a_changed_page_is_yielded():
    respx.get("http://d.test/company/acme").mock(
        side_effect=[
            httpx.Response(200, text="<html>before</html>"),
            httpx.Response(200, text="<html>after</html>"),
        ]
    )
    crawler = DirectoryCrawler(fast_settings())

    first = [
        listing async for listing in crawler.fetch_details(["http://d.test/company/acme"], "demo")
    ]
    known = {first[0].source_id: first[0].content_hash}
    second = [
        listing
        async for listing in crawler.fetch_details(
            ["http://d.test/company/acme"], "demo", seen_hashes=known
        )
    ]

    assert len(second) == 1
    assert second[0].content_hash != first[0].content_hash
    await crawler.aclose()


def test_the_activity_and_the_crawler_derive_source_ids_the_same_way():
    """One function, used by both. Two would silently never agree.

    The activity looks hashes up by source_id; the crawler writes records under
    source_id. If those two derivations drifted, every lookup would miss and
    incremental crawling would stop working with nothing in the logs.
    """
    for url, expected in [
        ("http://d.test/company/acme", "acme"),
        ("http://d.test/company/acme/", "acme"),
        ("http://d.test/company/acme?ref=x", "acme"),
        ("http://d.test/company/acme/?utm=y", "acme"),
    ]:
        assert source_id_from_url(url) == expected


# --------------------------------------------------------------------------
# force_refetch
# --------------------------------------------------------------------------


@respx.mock
async def test_force_refetch_does_not_even_ask_the_index():
    """The escape hatch for when the extractor changed, not the page.

    Asking and discarding would still cost an mget per batch, and would still
    be wrong the moment the index were unreachable.
    """
    from directory_pipeline.orchestration.activities import PipelineActivities

    respx.get("http://d.test/company/acme").mock(
        return_value=httpx.Response(200, text="<html><h1 class='company-name'>Acme</h1></html>")
    )

    activities = PipelineActivities(fast_settings(extraction_mode="dom"))
    asked: list[tuple[str, list[str]]] = []

    async def spy(source: str, source_ids: list[str], **_: Any) -> dict[str, str]:
        asked.append((source, source_ids))
        return {}

    activities.index.content_hashes = spy  # type: ignore[method-assign]

    try:
        await activities.fetch_and_extract(
            ["http://d.test/company/acme"], "demo", force_refetch=True
        )
        assert asked == [], "force_refetch must skip the lookup, not discard its result"

        await activities.fetch_and_extract(["http://d.test/company/acme"], "demo")
        assert asked == [("demo", ["acme"])]
    finally:
        await activities.aclose()


@respx.mock
async def test_the_activity_skips_a_page_the_index_already_has():
    from directory_pipeline.orchestration.activities import PipelineActivities

    body = "<html><h1 class='company-name'>Acme</h1></html>"
    respx.get("http://d.test/company/acme").mock(return_value=httpx.Response(200, text=body))

    activities = PipelineActivities(fast_settings(extraction_mode="dom"))

    # Stands in for the index across both calls, so neither reaches a cluster:
    # empty on the first (nothing indexed yet), then holding what the first
    # crawl would have written.
    store: dict[str, str] = {}

    async def stored(source: str, source_ids: list[str], **_: Any) -> dict[str, str]:
        return {sid: store[sid] for sid in source_ids if sid in store}

    activities.index.content_hashes = stored  # type: ignore[method-assign]

    try:
        records = await activities.fetch_and_extract(["http://d.test/company/acme"], "demo")
        assert len(records) == 1, "first crawl has no history, so it extracts"
        assert records[0].content_hash

        store[records[0].source_id] = records[0].content_hash

        again = await activities.fetch_and_extract(["http://d.test/company/acme"], "demo")
        assert again == [], "a page the index already has must not be re-extracted"
    finally:
        await activities.aclose()


def test_force_refetch_survives_continue_as_new():
    """The continuation carries the flag, or a long crawl changes behaviour
    halfway through at the run boundary."""
    from directory_pipeline.domain.models import CrawlRequest

    request = CrawlRequest(force_refetch=True)
    carried = request.model_copy(
        update={"categories": [], "max_pages": 0, "pending_urls": ["http://d.test/c/a"]}
    )
    assert carried.force_refetch is True


@pytest.mark.parametrize("flag", [True, False])
def test_force_refetch_is_a_plain_bool_on_the_request(flag: bool):
    from directory_pipeline.domain.models import CrawlRequest

    assert CrawlRequest(force_refetch=flag).force_refetch is flag
