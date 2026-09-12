"""Workflow tests using Temporal's in-memory test environment.

No server, no Docker. Activities are replaced by fakes so the test exercises the
thing that actually needs testing: the orchestration logic -- fan-out, result
aggregation, partial-failure handling, and the stop signal.

`WorkflowEnvironment.start_time_skipping()` fast-forwards timers, so retry
backoffs that would take minutes in real time complete instantly.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from directory_pipeline.domain.models import (
    Address,
    CompanyRecord,
    Contact,
    CrawlRequest,
    EnrichedCompany,
    Enrichment,
    ReindexRequest,
    ReindexResult,
)
from directory_pipeline.orchestration.workflows import (
    CrawlDirectoryWorkflow,
    ProcessBatchWorkflow,
    ReindexWorkflow,
)

TASK_QUEUE = "test-queue"


def fake_record(index: int) -> CompanyRecord:
    return CompanyRecord(
        record_id=CompanyRecord.make_record_id("test", f"c{index}"),
        source="test",
        source_id=f"c{index}",
        source_url=f"http://test/company/c{index}",
        name=f"Company {index}",
        name_normalized=f"company {index}",
        address=Address(city="Austin", region="TX"),
        contact=Contact(website=f"https://c{index}.example.com"),
    )


class FakeActivities:
    """Records what the workflow asked for, so the test can assert on it.

    Failure injection is keyed on URL, not on attempt count, so a "failing"
    batch fails on *every* retry. Keying on attempt count would only simulate a
    transient blip, which the retry policy legitimately recovers from -- and
    then the test would be asserting that retries do not work.
    """

    def __init__(self, *, poison_url: str | None = None, flaky_attempts: int = 0) -> None:
        self.poison_url = poison_url  # permanent failure
        self.flaky_attempts = flaky_attempts  # transient failures, then success
        self.indexed: list[EnrichedCompany] = []
        self.batch_calls = 0
        self.attempts = 0

    @activity.defn(name="discover_listings")
    async def discover_listings(self, category: str, max_pages: int) -> list[str]:
        return [f"http://test/company/{category}-{i}" for i in range(max_pages * 2)]

    @activity.defn(name="fetch_and_extract")
    async def fetch_and_extract(
        self, urls: list[str], source: str, seen_hashes: dict[str, str] | None = None
    ) -> list[CompanyRecord]:
        self.attempts += 1
        if self.poison_url and any(self.poison_url in u for u in urls):
            raise RuntimeError("simulated permanent upstream failure")
        if self.flaky_attempts and self.attempts <= self.flaky_attempts:
            raise RuntimeError("simulated transient upstream failure")
        self.batch_calls += 1
        return [fake_record(hash(u) % 10_000) for u in urls]

    @activity.defn(name="enrich_records")
    async def enrich_records(self, records: list[CompanyRecord]) -> list[EnrichedCompany]:
        return [
            EnrichedCompany(
                company=r,
                enrichment=Enrichment(provider="fake", industry="Software", confidence=0.9),
            )
            for r in records
        ]

    @activity.defn(name="resolve_duplicates")
    async def resolve_duplicates(
        self, companies: list[EnrichedCompany], adjudicate: bool = True
    ) -> list[EnrichedCompany]:
        return [c.model_copy(update={"cluster_id": c.company.record_id}) for c in companies]

    @activity.defn(name="bootstrap_index")
    async def bootstrap_index(self, alias: str) -> str:
        return f"{alias}-v3-test"

    @activity.defn(name="index_documents")
    async def index_documents(
        self, companies: list[EnrichedCompany], alias: str | None = None
    ) -> int:
        self.indexed.extend(companies)
        return len(companies)

    @activity.defn(name="reindex_alias")
    async def reindex_alias(self, request: ReindexRequest) -> ReindexResult:
        return ReindexResult(
            alias=request.alias,
            source_index=f"{request.alias}-v2-old",
            target_index=f"{request.alias}-v3-new",
            documents_copied=42,
            swapped=True,
            duration_s=0.1,
        )

    @activity.defn(name="index_stats")
    async def index_stats(self, alias: str) -> dict:
        return {"alias": alias, "index": f"{alias}-v3-test", "documents": len(self.indexed)}

    def all(self) -> list:
        return [
            self.discover_listings,
            self.fetch_and_extract,
            self.enrich_records,
            self.resolve_duplicates,
            self.bootstrap_index,
            self.index_documents,
            self.reindex_alias,
            self.index_stats,
        ]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


async def run_crawl(env: WorkflowEnvironment, activities: FakeActivities, request: CrawlRequest):
    client: Client = env.client
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CrawlDirectoryWorkflow, ProcessBatchWorkflow, ReindexWorkflow],
        activities=activities.all(),
    ):
        return await client.execute_workflow(
            CrawlDirectoryWorkflow.run,
            request,
            id=f"crawl-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


async def test_crawl_runs_end_to_end(env):
    activities = FakeActivities()
    result = await run_crawl(
        env,
        activities,
        CrawlRequest(categories=["software"], max_pages=2, batch_size=2),
    )

    assert result.listings_found == 4
    assert result.records_extracted == 4
    assert result.documents_indexed == 4
    assert result.index_name == "companies-v3-test"
    assert result.failures == []
    assert result.finished_at is not None


async def test_fan_out_creates_one_child_per_batch(env):
    activities = FakeActivities()
    await run_crawl(
        env,
        activities,
        CrawlRequest(categories=["software"], max_pages=5, batch_size=2),
    )
    # 10 urls / batch_size 2 -> 5 child workflows
    assert activities.batch_calls == 5


async def test_urls_are_deduped_across_categories(env):
    """Two categories returning the same URL must not be crawled twice."""

    class DuplicateDiscovery(FakeActivities):
        @activity.defn(name="discover_listings")
        async def discover_listings(self, category: str, max_pages: int) -> list[str]:
            return ["http://test/company/shared", "http://test/company/shared"]

    activities = DuplicateDiscovery()
    result = await run_crawl(
        env,
        activities,
        CrawlRequest(categories=["software", "logistics"], max_pages=1, batch_size=10),
    )
    assert result.listings_found == 1


async def test_one_permanently_failing_batch_does_not_sink_the_crawl(env):
    """The reason batches are child workflows: blast-radius containment.

    One batch fails on every attempt and exhausts its retries. The crawl must
    still finish, report that batch as a failure, and index the rest.
    """
    activities = FakeActivities(poison_url="software-0")
    result = await run_crawl(
        env,
        activities,
        CrawlRequest(categories=["software"], max_pages=3, batch_size=2),
    )

    assert result.failures, "the failed batch should be reported"
    assert result.documents_indexed > 0, "surviving batches should still index"
    # 3 batches, one poisoned -> the other two still land.
    assert result.records_extracted == 4


async def test_transient_activity_failure_is_retried_not_reported(env):
    """The complement: a blip must be absorbed by the retry policy silently.

    If this ever starts reporting a failure, the retry policy has stopped
    doing its job.
    """
    activities = FakeActivities(flaky_attempts=2)
    result = await run_crawl(
        env,
        activities,
        CrawlRequest(categories=["software"], max_pages=1, batch_size=10),
    )

    assert result.failures == []
    assert result.documents_indexed == 2
    assert activities.attempts == 3, "two failures then a success"


async def test_enrich_false_skips_the_enrichment_activity(env):
    class NoEnrichment(FakeActivities):
        @activity.defn(name="enrich_records")
        async def enrich_records(self, records):
            raise AssertionError("enrichment must not be called when enrich=False")

    result = await run_crawl(
        env,
        NoEnrichment(),
        CrawlRequest(categories=["software"], max_pages=1, batch_size=10, enrich=False),
    )
    assert result.records_enriched == 0
    assert result.documents_indexed == 2


async def test_empty_batch_short_circuits(env):
    class NoRecords(FakeActivities):
        @activity.defn(name="fetch_and_extract")
        async def fetch_and_extract(self, urls, source, seen_hashes=None):
            return []

        @activity.defn(name="index_documents")
        async def index_documents(self, companies, alias=None):
            raise AssertionError("indexing must not run for an empty batch")

    result = await run_crawl(env, NoRecords(), CrawlRequest(categories=["software"], max_pages=1))
    assert result.records_extracted == 0
    assert result.documents_indexed == 0


async def test_continuation_run_uses_pending_urls_and_skips_discovery(env):
    """A continue-as-new run must process the work it was handed.

    The overflow URLs are the whole reason the continuation exists; dropping
    them would silently lose everything past the per-run budget.
    """

    class NoDiscovery(FakeActivities):
        @activity.defn(name="discover_listings")
        async def discover_listings(self, category: str, max_pages: int) -> list[str]:
            raise AssertionError("a continuation must not re-discover")

    activities = NoDiscovery()
    result = await run_crawl(
        env,
        activities,
        CrawlRequest(
            categories=[],
            max_pages=0,
            batch_size=10,
            pending_urls=["http://test/company/a", "http://test/company/b"],
        ),
    )

    assert result.documents_indexed == 2
    assert result.failures == []


async def test_carried_totals_are_added_to_the_continuation_result(env):
    """Counts must span the whole crawl, not just the final run."""
    result = await run_crawl(
        env,
        FakeActivities(),
        CrawlRequest(
            categories=[],
            max_pages=0,
            batch_size=10,
            pending_urls=["http://test/company/a"],
            carried_totals={"records_extracted": 100, "documents_indexed": 90},
        ),
    )

    assert result.records_extracted == 101  # 100 carried + 1 this run
    assert result.documents_indexed == 91


async def test_reindex_workflow_returns_the_swap_result(env):
    activities = FakeActivities()
    client: Client = env.client
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ReindexWorkflow],
        activities=activities.all(),
    ):
        result = await client.execute_workflow(
            ReindexWorkflow.run,
            ReindexRequest(alias="companies", reason="mapping change"),
            id=f"reindex-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )

    assert result.swapped is True
    assert result.source_index == "companies-v2-old"
    assert result.target_index == "companies-v3-new"
    assert result.old_index_dropped is False  # rollback path preserved by default


async def test_batch_workflow_exposes_its_stage_via_query(env):
    activities = FakeActivities()
    client: Client = env.client
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ProcessBatchWorkflow],
        activities=activities.all(),
    ):
        handle = await client.start_workflow(
            ProcessBatchWorkflow.run,
            args=[["http://test/company/a"], "test", True, "companies", {}],
            id=f"batch-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        await handle.result()
        assert await handle.query("stage") == "done"
