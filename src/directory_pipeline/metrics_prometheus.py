"""Prometheus exposition for the in-process metrics.

Why this fixes what /metrics/summary could not: Prometheus scrapes each process
and aggregates at query time. There is no shared counter store to build, and no
process has to see another's numbers -- `sum(rate(...))` over the scrape targets
is the fleet view. What it requires instead is that every process be scrapeable,
which is why the worker now serves a port of its own.

The collector reads METRICS on each scrape rather than mirroring into a second
set of objects, so every existing call site stays exactly as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, HistogramMetricFamily
from prometheus_client.registry import Collector

from .observability import METRICS as METRICS
from .observability import get_logger

log = get_logger(__name__)

NAMESPACE = "directory_pipeline"
_ILLEGAL = re.compile(r"[^a-zA-Z0-9_]")


def metric_name(raw: str, *, suffix: str) -> str:
    """`crawl.index_pages` -> `directory_pipeline_crawl_index_pages_total`."""
    return f"{NAMESPACE}_{_ILLEGAL.sub('_', raw)}{suffix}"


class PipelineCollector(Collector):
    """Renders METRICS on demand. Registered once per process."""

    def collect(self) -> Iterable[Any]:
        yield from self._counters()
        yield from self._histograms()

    def _counters(self) -> Iterable[CounterMetricFamily]:
        # Prometheus requires every sample of a metric to carry the same label
        # names, but a counter can be incremented with different labels in
        # different places (http.response with a status, say, and without).
        # Grouping by name and filling the union with "" keeps the output valid.
        grouped: dict[str, list[tuple[dict[str, str], float]]] = {}
        for entry in METRICS.snapshot()["counters"]:
            grouped.setdefault(entry["name"], []).append((entry["labels"], entry["value"]))

        for raw, samples in sorted(grouped.items()):
            keys = sorted({k for labels, _ in samples for k in labels})
            family = CounterMetricFamily(
                metric_name(raw, suffix="_total"),
                f"Pipeline counter {raw}",
                labels=keys or None,
            )
            for labels, value in samples:
                if keys:
                    family.add_metric([labels.get(k, "") for k in keys], value)
                else:
                    family.add_metric([], value)
            yield family

    def _histograms(self) -> Iterable[HistogramMetricFamily]:
        for raw, data in sorted(METRICS.histograms().items()):
            yield HistogramMetricFamily(
                metric_name(raw, suffix="_seconds"),
                f"Pipeline latency {raw}",
                buckets=[(edge, float(count)) for edge, count in data["buckets"]],
                sum_value=data["sum"],
            )


def build_registry() -> CollectorRegistry:
    """A registry holding only our collector.

    Deliberately not the global default: that one carries process and GC
    collectors whose names would collide across the API and the workers, and
    this endpoint is about the pipeline, not the interpreter.
    """
    registry = CollectorRegistry()
    registry.register(PipelineCollector())
    return registry


def render(registry: CollectorRegistry | None = None) -> tuple[bytes, str]:
    """Exposition text plus the content type Prometheus expects."""
    return generate_latest(registry or build_registry()), CONTENT_TYPE_LATEST


def serve(port: int, addr: str = "0.0.0.0") -> None:
    """Expose a scrape endpoint from a process that has no HTTP server.

    The workers do the pipeline's actual work, so a metrics endpoint served only
    by the API reports a healthy pipeline as idle. This is the other half of the
    fix.
    """
    from prometheus_client import start_http_server

    start_http_server(port, addr=addr, registry=build_registry())
    log.info("metrics.prometheus_serving", port=port, addr=addr)
