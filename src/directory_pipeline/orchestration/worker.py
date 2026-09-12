"""Worker entrypoint.

One worker process serves both workflows and activities here. At real scale you
split them: workflow tasks are CPU-light and latency-sensitive, activity tasks
are I/O-heavy and bursty, so they autoscale on completely different signals.
Separate task queues let you scale scrapers to 200 pods while workflow workers
stay at 3.

`max_concurrent_activities` is the backpressure knob that actually matters.
Temporal will happily hand a worker more work than it can do; this is what stops
a pod from OOMing on 5,000 in-flight page fetches.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from ..config import get_settings
from ..observability import configure_logging, get_logger
from .activities import PipelineActivities
from .workflows import (
    CrawlDirectoryWorkflow,
    ProcessBatchWorkflow,
    ReindexWorkflow,
    ScheduledCrawlWorkflow,
)

log = get_logger(__name__)


async def connect(settings: Any = None) -> Client:
    """Connect with the Pydantic converter so models cross the wire as models."""
    settings = settings or get_settings()
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )


async def run_worker() -> None:
    settings = get_settings()
    configure_logging()
    client = await connect(settings)
    activities = PipelineActivities(settings)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            CrawlDirectoryWorkflow,
            ProcessBatchWorkflow,
            ReindexWorkflow,
            ScheduledCrawlWorkflow,
        ],
        activities=[
            activities.discover_listings,
            activities.fetch_and_extract,
            activities.fetch_one,
            activities.enrich_records,
            activities.resolve_duplicates,
            activities.bootstrap_index,
            activities.index_documents,
            activities.reindex_alias,
            activities.index_stats,
        ],
        max_concurrent_activities=settings.crawl_concurrency * 4,
        max_concurrent_workflow_tasks=100,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Graceful drain: stop accepting new tasks, let in-flight ones finish.
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "worker.starting",
        task_queue=settings.temporal_task_queue,
        address=settings.temporal_address,
    )
    try:
        await worker.run(shutdown_event=stop)
    finally:
        await activities.aclose()
        log.info("worker.stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
