"""Prometheus exposition.

The point of this exporter is aggregation across processes: the API and each
worker replica are scraped separately and summed at query time, which is what
/metrics/summary could not do. That only works if the output is valid and if the
latency data is in a form that can actually be added up -- a per-process p95 is
not summable, a bucket count is.
"""

from __future__ import annotations

from prometheus_client.parser import text_string_to_metric_families

from directory_pipeline.metrics_prometheus import build_registry, metric_name, render
from directory_pipeline.observability import _Metrics


def render_for(metrics: _Metrics) -> str:
    """Render a throwaway registry bound to a specific _Metrics instance."""
    import directory_pipeline.metrics_prometheus as mod

    original = mod.METRICS
    mod.METRICS = metrics
    try:
        body, _ = render(build_registry())
        return body.decode()
    finally:
        mod.METRICS = original


def test_names_are_sanitised_and_suffixed():
    assert metric_name("crawl.index_pages", suffix="_total") == (
        "directory_pipeline_crawl_index_pages_total"
    )
    assert metric_name("extract.llm", suffix="_seconds") == (
        "directory_pipeline_extract_llm_seconds"
    )


def test_the_output_parses_as_prometheus_exposition():
    m = _Metrics()
    m.incr("index.documents", 7)
    m.observe("crawl.detail_page", 0.4)
    families = list(text_string_to_metric_families(render_for(m)))
    names = {f.name for f in families}
    assert "directory_pipeline_index_documents" in names
    assert "directory_pipeline_crawl_detail_page_seconds" in names


def test_a_counter_incremented_with_and_without_labels_stays_valid():
    """Prometheus requires one label set per metric name.

    http.response carries a status in one place and not another, and emitting
    two different label sets under one name produces output a scraper rejects.
    """
    m = _Metrics()
    m.incr("http.response", 3, status="200")
    m.incr("http.response", 1)  # no label
    text = render_for(m)

    families = [
        f
        for f in text_string_to_metric_families(text)
        if f.name == "directory_pipeline_http_response"
    ]
    assert len(families) == 1
    label_sets = [tuple(sorted(s.labels)) for s in families[0].samples]
    assert len(set(label_sets)) == 1, f"inconsistent label names: {label_sets}"


def test_latency_is_a_histogram_so_replicas_can_be_summed():
    """Buckets add across processes; percentiles do not.

    Averaging two workers' p95 is not the fleet's p95, which is why the exporter
    emits buckets even though the JSON endpoint reports percentiles.
    """
    m = _Metrics()
    for seconds in (0.003, 0.03, 0.3, 3.0, 300.0):
        m.observe("crawl.detail_page", seconds)

    family = next(
        f
        for f in text_string_to_metric_families(render_for(m))
        if f.name == "directory_pipeline_crawl_detail_page_seconds"
    )
    assert family.type == "histogram"

    buckets = {s.labels["le"]: s.value for s in family.samples if s.name.endswith("_bucket")}
    # Cumulative: each bound includes everything below it.
    assert buckets["0.005"] == 1
    assert buckets["0.05"] == 2
    assert buckets["0.5"] == 3
    assert buckets["5.0"] == 4
    assert buckets["+Inf"] == 5

    count = next(s.value for s in family.samples if s.name.endswith("_count"))
    total = next(s.value for s in family.samples if s.name.endswith("_sum"))
    assert count == 5
    assert round(total, 3) == 303.333


def test_bucket_counters_never_decrease_when_the_sample_window_trims():
    """observe() trims its raw series at 5000 to bound memory.

    The buckets must not be trimmed with it: a counter that goes backwards makes
    every rate() computed over it wrong.
    """
    m = _Metrics()
    for _ in range(5200):
        m.observe("crawl.detail_page", 0.1)

    # The raw series is capped, which is the behaviour the buckets must survive.
    assert m.snapshot()["timings"]["crawl.detail_page"]["count"] == 5000

    hist = m.histograms()["crawl.detail_page"]
    assert hist["count"] == 5200, "bucket totals were trimmed with the raw series"
    assert hist["buckets"][-1] == ("+Inf", 5200)


def test_an_empty_process_renders_without_error():
    """A worker scraped before it has done any work must not fail the scrape."""
    text = render_for(_Metrics())
    assert list(text_string_to_metric_families(text)) == []
