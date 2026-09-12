"""Directory crawl: paginated index -> detail pages.

Two concurrency limits apply and they are not the same thing:

  * the rate limiter caps requests *per second* against the host (politeness)
  * the semaphore caps requests *in flight* (our own memory and socket budget)

A host that responds slowly would otherwise let thousands of coroutines pile up
under a low RPS limit, each holding a connection and a response body.

`seen_hashes` supports incremental re-crawls: a page whose body is unchanged
since the last run is skipped before extraction, which is where the real cost
(LLM calls) lives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..config import Settings
from ..domain.models import RawListing
from ..observability import METRICS, get_logger, timed
from .client import FetchError, ResilientClient

log = get_logger(__name__)


class DirectoryCrawler:
    """Crawls the demo directory. Swap the selectors for a real target."""

    LISTING_LINK_SELECTOR = "a.listing-link, .listing a[href*='/company/']"
    NEXT_PAGE_SELECTOR = "a[rel=next], a.next-page"

    def __init__(self, settings: Settings, client: ResilientClient | None = None) -> None:
        self.settings = settings
        self.client = client or ResilientClient(settings)
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(settings.crawl_concurrency)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def index_url(self, category: str, page: int) -> str:
        base = self.settings.directory_base_url.rstrip("/")
        return f"{base}/directory/{category}?page={page}"

    async def discover(self, category: str, max_pages: int) -> list[str]:
        """Walk pagination, collecting detail URLs.

        Sequential by design: page N+1's URL is only known after page N is
        parsed. Parallelism belongs in `fetch_details`, where it actually helps.
        """
        urls: list[str] = []
        seen: set[str] = set()
        next_url: str | None = self.index_url(category, 1)
        pages = 0

        while next_url and pages < max_pages:
            try:
                with timed("crawl.index_page"):
                    response = await self.client.get(next_url)
            except FetchError as exc:
                log.warning("crawl.index_failed", url=next_url, error=str(exc))
                break

            tree = HTMLParser(response.text)
            found = 0
            for node in tree.css(self.LISTING_LINK_SELECTOR):
                href = node.attributes.get("href")
                if not href:
                    continue
                absolute = urljoin(response.url, href)
                if absolute not in seen:
                    seen.add(absolute)
                    urls.append(absolute)
                    found += 1

            pages += 1
            METRICS.incr("crawl.index_pages")
            METRICS.incr("crawl.listings_found", found)
            log.info("crawl.index_page", url=next_url, found=found, page=pages)

            next_node = tree.css_first(self.NEXT_PAGE_SELECTOR)
            next_href = next_node.attributes.get("href") if next_node else None
            next_url = urljoin(response.url, next_href) if next_href else None

        return urls

    async def fetch_detail(self, url: str, source: str) -> RawListing:
        async with self._semaphore:
            with timed("crawl.detail_page"):
                response = await self.client.get(url)
        return RawListing(
            source=source,
            source_id=_source_id_from_url(url),
            url=response.url,
            html=response.text,
            http_status=response.status,
            via_proxy=response.via_proxy,
        )

    async def fetch_details(
        self,
        urls: list[str],
        source: str,
        *,
        seen_hashes: dict[str, str] | None = None,
    ) -> AsyncIterator[RawListing]:
        """Yield listings as they land, so extraction starts before the crawl ends."""
        seen_hashes = seen_hashes or {}
        tasks = {asyncio.create_task(self.fetch_detail(url, source)): url for url in urls}
        for task in asyncio.as_completed(tasks):
            try:
                listing = await task
            except FetchError as exc:
                METRICS.incr("crawl.detail_failed")
                log.warning("crawl.detail_failed", error=str(exc))
                continue
            if seen_hashes.get(listing.source_id) == listing.content_hash:
                METRICS.incr("crawl.unchanged_skipped")
                continue
            yield listing


def _source_id_from_url(url: str) -> str:
    """Last non-empty path segment. Stable across query-string churn."""
    path = url.split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] or path
