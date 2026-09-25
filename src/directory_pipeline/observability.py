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

# Counters are cumulative with no timestamps, so every rate below is "since this
# process started". That is the honest window: a worker restarted an hour ago
# cannot report yesterday's throughput.
PROCESS_STARTED = time.monotonic()


def _counter_totals(snapshot: dict[str, Any]) -> dict[str, float]:
    """Collapse labelled counters to one total per name."""
    totals: dict[str, float] = defaultdict(float)
    for entry in snapshot.get("counters", []):
        totals[entry["name"]] += entry["value"]
    return dict(totals)


def _ratio(numerator: float, denominator: float) -> float | None:
    """None rather than 0.0 when there is nothing to divide by.

    A cache hit rate of 0% and no lookups at all are different facts, and
    reporting the second as the first makes a cold process look broken.
    """
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def summarize(
    snapshot: dict[str, Any],
    *,
    elapsed_s: float | None = None,
    cost_per_mtok: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Derive the numbers a person actually asks for from the raw counters.

    The raw snapshot answers "how many", which leaves the reader doing division.
    This answers "how fast", "how often did we need the model", and "what did it
    cost" -- the questions that decide whether to change anything.

    Cost is omitted unless rates are supplied. Token prices change, and a stale
    hardcoded rate reported as fact is worse than no figure at all.
    """
    c = _counter_totals(snapshot)
    elapsed = elapsed_s if elapsed_s is not None else (time.monotonic() - PROCESS_STARTED)
    minutes = elapsed / 60 if elapsed > 0 else None

    indexed = c.get("index.documents", 0.0)
    llm_in = c.get("extract.llm_input_tokens", 0.0)
    llm_out = c.get("extract.llm_output_tokens", 0.0)
    cache_read = c.get("extract.llm_cache_read_tokens", 0.0)

    # Every counter here belongs to the process that serves the request. The
    # pipeline's work happens in worker processes, so an API-served summary sees
    # none of it and would otherwise report a healthy pipeline as doing nothing.
    # Saying so is the difference between an incomplete number and a wrong one.
    pipeline_counted = any(
        c.get(name)
        for name in (
            "index.documents",
            "crawl.index_pages",
            "extract.llm_repaired",
            "enrich.lookup_ok",
        )
    )

    out: dict[str, Any] = {
        "window_s": round(elapsed, 1),
        "scope": {
            "counters_are_per_process": True,
            "sees_pipeline_activity": bool(pipeline_counted),
            "note": (
                "In-memory counters cover only the process answering this "
                "request. Extraction, enrichment and indexing run in worker "
                "processes, so those sections read zero here unless this is a "
                "worker. A shared collector is what makes them whole."
                if not pipeline_counted
                else "In-memory counters cover only the process answering this request."
            ),
        },
        "throughput": {
            "records_indexed": int(indexed),
            "records_per_minute": round(indexed / minutes, 2) if minutes else None,
            "pages_fetched": int(c.get("crawl.index_pages", 0)),
            "unchanged_skipped": int(c.get("crawl.unchanged_skipped", 0)),
            # What the content hash is buying: pages that never reached the
            # extractor at all.
            "skip_rate": _ratio(
                c.get("crawl.unchanged_skipped", 0.0),
                c.get("crawl.unchanged_skipped", 0.0) + c.get("crawl.index_pages", 0.0),
            ),
        },
        "extraction": {
            # Layer 3's share of the work, which is the number that justifies
            # its cost. The eval measures the same thing offline.
            "records_repaired_by_model": int(c.get("extract.llm_repaired", 0)),
            "model_share_of_records": _ratio(c.get("extract.llm_repaired", 0.0), indexed),
            "fields_filled_by_text_layer": int(c.get("extract.text_fallback_filled", 0)),
            "model_refusals": int(c.get("extract.llm_refusal", 0)),
            "model_truncations": int(c.get("extract.llm_truncated", 0)),
            "model_errors": int(c.get("extract.llm_error", 0)),
        },
        "model_tokens": {
            "input": int(llm_in),
            "output": int(llm_out),
            "cache_read": int(cache_read),
            # Prompt caching only pays if the prefix stays byte-identical. A
            # collapse here is the visible symptom of that invariant breaking.
            "cache_read_share_of_input": _ratio(cache_read, llm_in),
        },
        "dependencies": {
            "enrichment_cache_hit_rate": _ratio(
                c.get("enrich.cache_hit", 0.0),
                c.get("enrich.cache_hit", 0.0) + c.get("enrich.lookup_ok", 0.0),
            ),
            "enrichment_failures": int(c.get("enrich.failed", 0)),
            "fetch_failures": int(c.get("crawl.detail_failed", 0)),
            "circuit_opens": int(c.get("http.circuit_open", 0)),
            "bulk_index_errors": int(c.get("index.bulk_errors", 0)),
        },
    }

    if cost_per_mtok:
        billed_input = max(llm_in - cache_read, 0.0)
        usd = (
            billed_input / 1e6 * cost_per_mtok.get("input", 0.0)
            + cache_read / 1e6 * cost_per_mtok.get("cache_read", 0.0)
            + llm_out / 1e6 * cost_per_mtok.get("output", 0.0)
        )
        out["cost"] = {
            "rates_per_mtok": cost_per_mtok,
            "estimated_usd": round(usd, 6),
            "estimated_usd_per_1k_records": (round(usd / indexed * 1000, 4) if indexed else None),
            "note": "Estimated from configured rates, not billed amounts.",
        }
    else:
        out["cost"] = {
            "estimated_usd": None,
            "note": (
                "Set LLM_COST_INPUT_PER_MTOK, LLM_COST_OUTPUT_PER_MTOK and "
                "LLM_COST_CACHE_READ_PER_MTOK to enable cost estimates. Left "
                "unset on purpose: a stale hardcoded price reported as fact is "
                "worse than no figure."
            ),
        }
    return out


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
