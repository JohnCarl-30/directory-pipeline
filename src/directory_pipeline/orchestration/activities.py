"""Activities: everything that touches the outside world.

Activities are grouped on a class so expensive clients (HTTP pools, the
OpenSearch connection) are built once per worker rather than once per activity
invocation. Temporal calls the bound methods; the worker owns the lifecycle.

Two rules every activity here follows:

  * **Heartbeat during long loops.** A heartbeat is what lets Temporal notice a
    dead worker in seconds instead of at the start_to_close timeout. It also
    carries progress, so a retry can resume rather than restart.

  * **Be idempotent.** Activities are *at-least-once*: a worker can complete
    the work, die before reporting, and have the whole activity re-run. Every
    write here is keyed on a deterministic id, so a double execution overwrites
    rather than duplicates.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ..config import Settings, get_settings
from ..domain.models import (
    CompanyRecord,
    EnrichedCompany,
    RawListing,
    ReindexRequest,
    ReindexResult,
)
from ..enrichment.provider import EnrichmentProvider
from ..extraction.agent import ExtractionError, Extractor
from ..observability import configure_logging, get_logger
from ..resolution.adjudicator import Adjudicator
from ..resolution.entity import Candidate, resolve
from ..scraping.client import FetchError, ResilientClient
from ..scraping.crawler import DirectoryCrawler
from ..search.index import SearchIndex

log = get_logger(__name__)


async def _reraise_fetch_errors(pages: Any) -> Any:
    """Classify a FetchError raised part-way through the page stream."""
    try:
        async for page in pages:
            yield page
    except FetchError as exc:
        raise _classified(exc) from exc


def _classified(exc: FetchError) -> ApplicationError:
    """Hand Temporal the client's own verdict on whether a retry can help.

    The scraping client already classifies failures -- RETRYABLE_STATUS, then its
    own backoff loop -- and only raises once it has either exhausted those
    attempts or seen something terminal. Letting a bare FetchError escape throws
    that away: the retry policy sees an unrecognised class name and retries a 404
    six times over ten minutes.

    Keying on exc.retryable rather than on a class name in NON_RETRYABLE also
    means the decision cannot silently rot. A name in that list stops matching
    the moment a class is renamed, with no error anywhere.
    """
    return ApplicationError(
        str(exc),
        type=type(exc).__name__,
        non_retryable=not getattr(exc, "retryable", False),
    )


class PipelineActivities:
    """Holds the shared clients. One instance per worker process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        configure_logging()
        self._crawl_client = ResilientClient(self.settings)
        self._enrich_client = ResilientClient(
            self.settings,
            rps=self.settings.enrich_rps,
            burst=max(1, int(self.settings.enrich_rps)),
        )
        self.crawler = DirectoryCrawler(self.settings, client=self._crawl_client)
        self.extractor = Extractor(self.settings)
        self.enricher = EnrichmentProvider(self.settings, client=self._enrich_client)
        self.adjudicator = Adjudicator(self.settings)
        self.index = SearchIndex(self.settings)

    async def aclose(self) -> None:
        await self._crawl_client.aclose()
        await self._enrich_client.aclose()
        await self.index.aclose()

    # --- discovery ---------------------------------------------------------

    @activity.defn(name="discover_listings")
    async def discover_listings(self, category: str, max_pages: int) -> list[str]:
        activity.heartbeat({"stage": "discover", "category": category})
        try:
            urls = await self.crawler.discover(category, max_pages)
        except FetchError as exc:
            raise _classified(exc) from exc
        log.info("activity.discovered", category=category, urls=len(urls))
        return urls

    # --- fetch + extract ---------------------------------------------------

    @activity.defn(name="fetch_and_extract")
    async def fetch_and_extract(
        self, urls: list[str], source: str, seen_hashes: dict[str, str] | None = None
    ) -> list[CompanyRecord]:
        """Fetch a batch of detail pages and extract records from each.

        Fetch and extract live in one activity on purpose: passing raw HTML
        between activities would push megabytes of page source through
        Temporal's payload limits and into workflow history forever.
        """
        records: list[CompanyRecord] = []
        processed = 0

        pages = self.crawler.fetch_details(urls, source, seen_hashes=seen_hashes or {})
        async for listing in _reraise_fetch_errors(pages):
            processed += 1
            try:
                records.append(await self.extractor.extract(listing))
            except ExtractionError as exc:
                # A single unparseable page must not fail the batch -- and it
                # must not be retried, because it will fail identically.
                log.warning("activity.extract_skipped", url=listing.url, error=str(exc))
            except Exception as exc:
                log.error("activity.extract_error", url=listing.url, error=str(exc))

            if processed % 5 == 0:
                activity.heartbeat({"stage": "extract", "processed": processed, "total": len(urls)})

        log.info("activity.extracted", requested=len(urls), extracted=len(records))
        return records

    @activity.defn(name="fetch_one")
    async def fetch_one(self, url: str, source: str) -> RawListing:
        try:
            return await self.crawler.fetch_detail(url, source)
        except FetchError as exc:
            raise _classified(exc) from exc

    # --- enrichment --------------------------------------------------------

    @activity.defn(name="enrich_records")
    async def enrich_records(self, records: list[CompanyRecord]) -> list[EnrichedCompany]:
        activity.heartbeat({"stage": "enrich", "count": len(records)})
        enrichments = await self.enricher.enrich_many(records)
        out = [
            EnrichedCompany(company=record, enrichment=enrichment)
            for record, enrichment in zip(records, enrichments, strict=True)
        ]
        hits = sum(1 for e in enrichments if e is not None)
        log.info("activity.enriched", records=len(records), enriched=hits)
        return out

    # --- resolution --------------------------------------------------------

    @activity.defn(name="resolve_duplicates")
    async def resolve_duplicates(
        self, companies: list[EnrichedCompany], adjudicate: bool = True
    ) -> list[EnrichedCompany]:
        """Cluster duplicates, optionally using the LLM on borderline pairs."""
        records = [c.company for c in companies]
        activity.heartbeat({"stage": "resolve", "count": len(records)})

        accepted: list[Candidate] = []
        if adjudicate and self.adjudicator.enabled:
            from ..resolution.entity import generate_candidates

            borderline = [c for c in generate_candidates(records) if c.needs_review]
            if borderline:
                log.info("activity.adjudicating", pairs=len(borderline))
                accepted = await self.adjudicator.adjudicate_many(borderline)

        cluster_of, canonical_of = resolve(records, accepted=accepted)

        out: list[EnrichedCompany] = []
        collapsed = 0
        for company in companies:
            rid = company.company.record_id
            canonical = canonical_of.get(rid, rid)
            if canonical != rid:
                collapsed += 1
            out.append(
                company.model_copy(
                    update={
                        "cluster_id": cluster_of.get(rid, rid),
                        "duplicate_of": None if canonical == rid else canonical,
                    }
                )
            )
        log.info("activity.resolved", records=len(records), duplicates=collapsed)
        return out

    # --- indexing ----------------------------------------------------------

    @activity.defn(name="bootstrap_index")
    async def bootstrap_index(self, alias: str) -> str:
        return await self.index.bootstrap(alias)

    @activity.defn(name="index_documents")
    async def index_documents(
        self, companies: list[EnrichedCompany], alias: str | None = None
    ) -> int:
        activity.heartbeat({"stage": "index", "count": len(companies)})
        return await self.index.index_documents(companies, alias=alias)

    @activity.defn(name="reindex_alias")
    async def reindex_alias(self, request: ReindexRequest) -> ReindexResult:
        activity.heartbeat({"stage": "reindex", "alias": request.alias})
        try:
            result = await self.index.reindex(
                alias=request.alias,
                wait_for_completion=request.wait_for_completion,
                drop_old_index=request.drop_old_index,
                # Each poll heartbeats with live copy progress, so a reindex
                # that outlives the heartbeat timeout is rescheduled rather
                # than silently declared dead.
                on_progress=lambda status: activity.heartbeat(
                    {"stage": "reindex", "alias": request.alias, **status}
                ),
            )
        except Exception as exc:
            raise ApplicationError(f"reindex failed: {exc}", type="ReindexError") from exc
        return ReindexResult(**result)

    @activity.defn(name="index_stats")
    async def index_stats(self, alias: str) -> dict[str, Any]:
        return await self.index.stats(alias)
