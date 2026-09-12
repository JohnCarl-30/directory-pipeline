"""Structured logging + lightweight metrics.

Logs are JSON so CloudWatch Insights can query them directly. Every log line
inside a workflow carries the workflow/run id, so a single failed company can be
traced from the crawl fetch through enrichment to the indexed document.

The metrics registry is deliberately tiny and in-process: it exists so the API
can expose /metrics without pulling in a full Prometheus client. Swap
`emit_metric` for an OTel meter when you wire a collector.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper())
    )
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


bind = structlog.contextvars.bind_contextvars
clear = structlog.contextvars.clear_contextvars


class _Metrics:
    """Counters + latency histograms, aggregated in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._timings: dict[str, list[float]] = defaultdict(list)

    def incr(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            series = self._timings[name]
            series.append(seconds)
            if len(series) > 5000:  # bound memory on long-lived workers
                del series[:-5000]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(self._counters.items())
            ]
            timings = {}
            for name, values in self._timings.items():
                if not values:
                    continue
                ordered = sorted(values)
                timings[name] = {
                    "count": len(ordered),
                    "p50": _pct(ordered, 0.50),
                    "p95": _pct(ordered, 0.95),
                    "p99": _pct(ordered, 0.99),
                    "max": ordered[-1],
                }
        return {"counters": counters, "timings": timings}


def _pct(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return round(ordered[idx], 6)


METRICS = _Metrics()


@contextmanager
def timed(name: str, **labels: str) -> Iterator[None]:
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        METRICS.observe(name, time.perf_counter() - start)
        METRICS.incr(f"{name}.count", outcome=outcome, **labels)
