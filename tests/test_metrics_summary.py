"""Derived metrics: the arithmetic /metrics leaves to the reader.

Raw counters answer "how many". These answer the questions someone asks of a
running pipeline -- how fast, how often did it need the model, is prompt caching
still working, what is it costing -- and those answers are worth testing because
each one is a division that can be wrong in a way that looks plausible.
"""

from __future__ import annotations

from directory_pipeline.config import Settings
from directory_pipeline.observability import summarize


def snap(**counters: float) -> dict:
    return {
        "counters": [
            {"name": name, "labels": {}, "value": value} for name, value in counters.items()
        ],
        "timings": {},
    }


def test_throughput_is_per_minute_over_the_window():
    out = summarize(snap(**{"index.documents": 120}), elapsed_s=180)
    assert out["throughput"]["records_indexed"] == 120
    assert out["throughput"]["records_per_minute"] == 40.0
    assert out["window_s"] == 180


def test_no_data_reads_as_none_not_zero_percent():
    """A 0% cache hit rate and no lookups at all are different facts.

    Reporting the second as the first makes a freshly started process look
    broken, which is how a real problem gets ignored.
    """
    out = summarize(snap(), elapsed_s=60)
    assert out["dependencies"]["enrichment_cache_hit_rate"] is None
    assert out["model_tokens"]["cache_read_share_of_input"] is None
    assert out["throughput"]["skip_rate"] is None


def test_a_genuine_zero_is_reported_as_zero():
    out = summarize(snap(**{"enrich.lookup_ok": 50}), elapsed_s=60)
    assert out["dependencies"]["enrichment_cache_hit_rate"] == 0.0


def test_model_share_of_records_is_the_number_that_justifies_layer_three():
    out = summarize(snap(**{"index.documents": 200, "extract.llm_repaired": 18}), elapsed_s=60)
    assert out["extraction"]["model_share_of_records"] == 0.09


def test_cost_is_absent_until_rates_are_configured():
    out = summarize(snap(**{"extract.llm_input_tokens": 1_000_000}), elapsed_s=60)
    assert out["cost"]["estimated_usd"] is None
    assert "Set LLM_COST_INPUT_PER_MTOK" in out["cost"]["note"]


def test_cached_tokens_are_billed_at_the_cache_rate_not_the_input_rate():
    """Billing cache reads as fresh input overstates cost several times over.

    With 90% of a prompt served from cache, charging all of it at the input rate
    is the difference between a pipeline that looks affordable and one that does
    not.
    """
    rates = {"input": 15.0, "output": 75.0, "cache_read": 1.5}
    out = summarize(
        snap(
            **{
                "extract.llm_input_tokens": 1_000_000,
                "extract.llm_cache_read_tokens": 900_000,
                "extract.llm_output_tokens": 100_000,
            }
        ),
        elapsed_s=60,
        cost_per_mtok=rates,
    )
    # 100k fresh input at 15, 900k cached at 1.5, 100k output at 75.
    expected = 0.1 * 15.0 + 0.9 * 1.5 + 0.1 * 75.0
    assert out["cost"]["estimated_usd"] == round(expected, 6)
    # Billing it all as fresh input would have produced a much larger number.
    assert out["cost"]["estimated_usd"] < 1.0 * 15.0 + 0.1 * 75.0


def test_cost_per_thousand_records_needs_records():
    rates = {"input": 15.0, "output": 75.0, "cache_read": 1.5}
    out = summarize(
        snap(**{"extract.llm_input_tokens": 1_000_000}), elapsed_s=60, cost_per_mtok=rates
    )
    assert out["cost"]["estimated_usd_per_1k_records"] is None

    out = summarize(
        snap(**{"extract.llm_input_tokens": 1_000_000, "index.documents": 500}),
        elapsed_s=60,
        cost_per_mtok=rates,
    )
    assert out["cost"]["estimated_usd_per_1k_records"] == 30.0


def test_labelled_counters_are_summed_not_dropped():
    """http.response carries a status label, so the same name appears many times."""
    snapshot = {
        "counters": [
            {"name": "index.documents", "labels": {"alias": "a"}, "value": 30},
            {"name": "index.documents", "labels": {"alias": "b"}, "value": 70},
        ],
        "timings": {},
    }
    assert summarize(snapshot, elapsed_s=60)["throughput"]["records_indexed"] == 100


def test_rates_property_is_none_until_configured():
    base = dict(
        directory_base_url="http://directory.test", enrichment_base_url="http://enrich.test"
    )
    assert Settings(**base).llm_cost_rates is None
    configured = Settings(**base, llm_cost_input_per_mtok=15.0)
    assert configured.llm_cost_rates == {"input": 15.0, "output": 0.0, "cache_read": 0.0}


def test_the_summary_admits_when_it_cannot_see_the_pipeline():
    """Served from the API, every pipeline counter is zero because the work
    happens in worker processes. Reporting that as a healthy zero would be
    worse than reporting nothing: 0 records/min reads as an outage.
    """
    api_only = summarize(snap(**{"api.crawls_started": 1}), elapsed_s=60)
    assert api_only["scope"]["sees_pipeline_activity"] is False
    assert "worker processes" in api_only["scope"]["note"]

    worker_side = summarize(snap(**{"index.documents": 7}), elapsed_s=60)
    assert worker_side["scope"]["sees_pipeline_activity"] is True
