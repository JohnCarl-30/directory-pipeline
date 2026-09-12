"""Workflows: deterministic orchestration only.

A workflow function is replayed from history after every worker restart, so it
must be a pure function of its inputs and past activity results. That means no
HTTP, no clock reads, no random, no file access -- all of that lives in
activities. `workflow.now()` and `workflow.random()` exist for the cases where
you genuinely need time or randomness and still need replay to be deterministic.

The shape here is a parent that fans out to child workflows, one per batch.
Children are not decoration: each gets its own history, its own retry budget,
and its own failure blast radius, so one poisoned batch of 25 companies cannot
take down a crawl of 50,000.

`continue_as_new` bounds history growth. A workflow that loops forever
accumulates events until it hits the history limit and dies; continue-as-new
starts a fresh run carrying only the state that matters.

**Activities are invoked by string name, so every typed call must pass
`result_type`.** Invoking by name is what lets activities be deployed and
versioned separately from workflows -- the workflow needs no import of the
activity implementation. The cost is that Temporal has no return annotation to
read, so without `result_type` a payload deserializes to a raw `dict` and the
first attribute access raises `AttributeError`. That failure is a *workflow task*
failure, which Temporal retries forever, so the symptom is a workflow that hangs
rather than one that errors. Pass `result_type` on every activity call returning
a model.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from ..domain.models import (
        CompanyRecord,
        CrawlRequest,
        CrawlResult,
        EnrichedCompany,
        ReindexRequest,
        ReindexResult,
    )
    from .shared import (
        API_RETRY,
        DISCOVER_TIMEOUTS,
        ENRICH_TIMEOUTS,
        FETCH_TIMEOUTS,
        INDEX_RETRY,
        INDEX_TIMEOUTS,
        REINDEX_RETRY,
        REINDEX_TIMEOUTS,
        SCRAPE_RETRY,
    )

# Above this many URLs, the parent hands the remainder to a fresh run rather
# than growing one history unboundedly.
MAX_URLS_PER_RUN = 2_000


@workflow.defn(name="ProcessBatchWorkflow")
class ProcessBatchWorkflow:
    """One batch: fetch -> extract -> enrich -> resolve -> index.

    Isolated as a child so a batch that fails after 6 retries fails alone.
    """

    def __init__(self) -> None:
        self._stage = "pending"

    @workflow.query(name="stage")
    def stage(self) -> str:
        """Queryable progress. `temporal workflow query --type stage` while it runs."""
        return self._stage

    @workflow.run
    async def run(
        self,
        urls: list[str],
        source: str,
        enrich: bool,
        alias: str | None,
        seen_hashes: dict[str, str] | None = None,
    ) -> dict[str, int]:
        self._stage = "fetching"
        records: list[CompanyRecord] = await workflow.execute_activity(
            "fetch_and_extract",
            args=[urls, source, seen_hashes or {}],
            result_type=list[CompanyRecord],
            retry_policy=SCRAPE_RETRY,
            **FETCH_TIMEOUTS,
        )
        if not records:
            self._stage = "empty"
            return {"extracted": 0, "enriched": 0, "duplicates": 0, "indexed": 0}

        if enrich:
            self._stage = "enriching"
            companies: list[EnrichedCompany] = await workflow.execute_activity(
                "enrich_records",
                args=[records],
                result_type=list[EnrichedCompany],
                retry_policy=API_RETRY,
                **ENRICH_TIMEOUTS,
            )
        else:
            companies = [EnrichedCompany(company=r) for r in records]

        self._stage = "resolving"
        companies = await workflow.execute_activity(
            "resolve_duplicates",
            args=[companies, True],
            result_type=list[EnrichedCompany],
            retry_policy=API_RETRY,
            **ENRICH_TIMEOUTS,
        )

        self._stage = "indexing"
        indexed: int = await workflow.execute_activity(
            "index_documents",
            args=[companies, alias],
            result_type=int,
            retry_policy=INDEX_RETRY,
            **INDEX_TIMEOUTS,
        )

        self._stage = "done"
        return {
            "extracted": len(records),
            "enriched": sum(1 for c in companies if c.enrichment is not None),
            "duplicates": sum(1 for c in companies if c.duplicate_of),
            "indexed": indexed,
        }


@workflow.defn(name="CrawlDirectoryWorkflow")
class CrawlDirectoryWorkflow:
    """Parent: discover URLs, fan out batches, aggregate results."""

    def __init__(self) -> None:
        self._result: CrawlResult | None = None
        self._progress = {"batches_total": 0, "batches_done": 0}
        self._cancelled = False

    @workflow.query(name="progress")
    def progress(self) -> dict[str, int]:
        return dict(self._progress)

    @workflow.signal(name="stop")
    def stop(self) -> None:
        """Graceful stop: finish in-flight batches, start no new ones."""
        self._cancelled = True

    @workflow.run
    async def run(self, request: CrawlRequest) -> CrawlResult:
        info = workflow.info()
        result = CrawlResult(run_id=info.run_id, started_at=workflow.now())
        alias = request.index_alias

        # Ensure the alias exists before anything writes through it.
        index_name: str = await workflow.execute_activity(
            "bootstrap_index",
            args=[alias or "companies"],
            result_type=str,
            retry_policy=INDEX_RETRY,
            **INDEX_TIMEOUTS,
        )
        result.index_name = index_name

        # Totals carried over from a previous run, so the final result covers
        # the whole crawl rather than only the last continuation.
        for field, value in (request.carried_totals or {}).items():
            setattr(result, field, getattr(result, field, 0) + value)

        if request.pending_urls:
            # Continuation run: this work was already discovered upstream.
            urls = list(request.pending_urls)
        else:
            # Discovery is sequential per category, parallel across categories.
            discovery = [
                workflow.execute_activity(
                    "discover_listings",
                    args=[category, request.max_pages],
                    result_type=list[str],
                    retry_policy=SCRAPE_RETRY,
                    **DISCOVER_TIMEOUTS,
                )
                for category in request.categories
            ]

            all_urls: list[str] = []
            for coro in discovery:
                try:
                    all_urls.extend(await coro)
                except ActivityError as exc:
                    result.failures.append(f"discovery failed: {exc}")

            # Dedupe while preserving order -- determinism matters on replay,
            # and a set alone would reorder between runs.
            seen: set[str] = set()
            urls = [u for u in all_urls if not (u in seen or seen.add(u))]
            result.listings_found += len(urls)
            result.pages_crawled += len(request.categories) * request.max_pages

        overflow: list[str] = []
        if len(urls) > MAX_URLS_PER_RUN:
            urls, overflow = urls[:MAX_URLS_PER_RUN], urls[MAX_URLS_PER_RUN:]

        batches = [
            urls[i : i + request.batch_size] for i in range(0, len(urls), request.batch_size)
        ]
        self._progress["batches_total"] = len(batches)

        # Fan out children, then await them. Starting them all first is what
        # makes this concurrent rather than a sequential loop with extra steps.
        handles = []
        for position, batch in enumerate(batches):
            if self._cancelled:
                result.failures.append(f"stopped by signal after {position} batches")
                break
            handles.append(
                await workflow.start_child_workflow(
                    ProcessBatchWorkflow.run,
                    args=[batch, request.source, request.enrich, alias, {}],
                    id=f"{info.workflow_id}-batch-{position}",
                    task_queue=info.task_queue,
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        initial_interval=timedelta(seconds=10),
                    ),
                )
            )

        for position, handle in enumerate(handles):
            try:
                counts = await handle
            except ChildWorkflowError as exc:
                result.failures.append(f"batch {position} failed: {exc}")
                continue
            finally:
                self._progress["batches_done"] += 1
            result.records_extracted += counts["extracted"]
            result.records_enriched += counts["enriched"]
            result.duplicates_collapsed += counts["duplicates"]
            result.documents_indexed += counts["indexed"]

        result.finished_at = workflow.now()
        self._result = result

        if overflow and not self._cancelled:
            # History would keep growing if we looped here. Hand the remainder
            # to a fresh run with a clean history -- carrying both the URLs
            # still to do and the totals so far, or the work and the counts
            # are lost at the boundary.
            workflow.logger.info("continuing as new for %d remaining urls", len(overflow))
            workflow.continue_as_new(
                args=[
                    request.model_copy(
                        update={
                            "categories": [],
                            "max_pages": 0,
                            "pending_urls": overflow,
                            "carried_totals": {
                                "listings_found": result.listings_found,
                                "pages_crawled": result.pages_crawled,
                                "records_extracted": result.records_extracted,
                                "records_enriched": result.records_enriched,
                                "duplicates_collapsed": result.duplicates_collapsed,
                                "documents_indexed": result.documents_indexed,
                            },
                        }
                    )
                ]
            )

        return result


@workflow.defn(name="ReindexWorkflow")
class ReindexWorkflow:
    """Zero-downtime reindex as a durable operation.

    Worth its own workflow rather than a shell script: a reindex can run for
    hours, and if the operator's laptop closes mid-run you want the swap to
    still happen, exactly once, with a record of what it did.
    """

    @workflow.run
    async def run(self, request: ReindexRequest) -> ReindexResult:
        return await workflow.execute_activity(
            "reindex_alias",
            args=[request],
            result_type=ReindexResult,
            retry_policy=REINDEX_RETRY,
            **REINDEX_TIMEOUTS,
        )


@workflow.defn(name="ScheduledCrawlWorkflow")
class ScheduledCrawlWorkflow:
    """Entry point for a Temporal Schedule (cron).

    A thin wrapper is the right shape here: the schedule owns cadence and
    overlap policy, the child owns the work, and the two can be reasoned about
    separately.
    """

    @workflow.run
    async def run(self, request: CrawlRequest) -> CrawlResult:
        return await workflow.execute_child_workflow(
            CrawlDirectoryWorkflow.run,
            args=[request],
            id=f"scheduled-crawl-{workflow.info().workflow_id}",
        )
