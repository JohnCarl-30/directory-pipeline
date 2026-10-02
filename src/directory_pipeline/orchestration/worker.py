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
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
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


_runtime: Runtime | None = None


def _metrics_runtime(settings: Any) -> Runtime | None:
    """A Runtime that publishes the SDK's own metrics, built at most once.

    These are the numbers the application counters cannot see: how long a task
    waits before a worker picks it up, how often an activity failed and was
    retried, how many pollers are alive. Schedule-to-start latency is the signal
    this service autoscales on -- queue depth, not CPU, because scrapers sit
    near-idle while the queue is deep.

    Cached because a Runtime binds a port; building a second one in the same
    process fails on the bind rather than on anything informative.
    """
    global _runtime
    if not settings.temporal_metrics_port:
        return None
    if _runtime is None:
        _runtime = Runtime(
            telemetry=TelemetryConfig(
                metrics=PrometheusConfig(
                    bind_address=f"0.0.0.0:{settings.temporal_metrics_port}",
                    # The SDK defaults predate Prometheus conventions: no _total
                    # on counters, no unit suffix, durations in milliseconds.
                    # Left alone, rate() over a counter and any duration maths
                    # silently disagree with every other metric in the stack.
                    counters_total_suffix=True,
                    unit_suffix=True,
                    durations_as_seconds=True,
                )
            )
        )
        log.info("temporal.metrics_serving", port=settings.temporal_metrics_port)
    return _runtime


async def connect(settings: Any = None) -> Client:
    """Connect with the Pydantic converter so models cross the wire as models."""
    settings = settings or get_settings()
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
        runtime=_metrics_runtime(settings),
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
            activities.enrich_records,
            activities.resolve_duplicates,
            activities.bootstrap_index,
            activities.index_documents,
            activities.reindex_alias,
        ],
        max_concurrent_activities=settings.crawl_concurrency * 4,
        max_concurrent_workflow_tasks=100,
        # A crawl activity can be mid-fetch when the pod is cycled. Give it
        # time to land rather than cancelling it and retrying the work.
        graceful_shutdown_timeout=timedelta(seconds=30),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Graceful drain: stop accepting new tasks, let in-flight ones finish.
        loop.add_signal_handler(sig, stop.set)

    if settings.worker_metrics_port:
        # Without this the worker's counters are unreachable: it has no HTTP
        # server of its own, and the pipeline's work all happens here.
        from ..metrics_prometheus import serve as serve_metrics

        serve_metrics(settings.worker_metrics_port)

    log.info(
        "worker.starting",
        task_queue=settings.temporal_task_queue,
        address=settings.temporal_address,
    )
    try:
        async with worker:
            await stop.wait()
    finally:
        await activities.aclose()
        log.info("worker.stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
