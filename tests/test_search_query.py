"""Query shape. These assertions encode relevance decisions, not syntax."""

from __future__ import annotations

from directory_pipeline.search.index import ANALYSIS, MAPPINGS, physical_index_name
from directory_pipeline.search.query import build_query, build_suggest_query


def _filters(body):
    return body["query"]["function_score"]["query"]["bool"]["filter"]


def test_filters_live_in_filter_context_not_must():
    """Filter context is cacheable and score-neutral. This is the whole point."""
    body = build_query(q="analytics", city="Austin", region="tx")
    must = body["query"]["function_score"]["query"]["bool"]["must"]
    assert len(must) == 1 and "multi_match" in must[0]
    assert {"term": {"address.city": "Austin"}} in _filters(body)
    assert {"term": {"address.region": "TX"}} in _filters(body)


def test_canonical_filter_is_on_by_default():
    assert {"term": {"is_canonical": True}} in _filters(build_query(q="x"))


def test_include_duplicates_removes_the_canonical_filter():
    body = build_query(q="x", canonical_only=False)
    assert {"term": {"is_canonical": True}} not in _filters(body)


def test_name_outranks_description():
    fields = build_query(q="x")["query"]["function_score"]["query"]["bool"]["must"][0][
        "multi_match"
    ]["fields"]
    boosts = {f.split("^")[0]: float(f.split("^")[1]) for f in fields if "^" in f}
    assert boosts["name"] > boosts["categories"] > boosts["description"]


def test_technologies_are_anded_not_ored():
    """Asking for Kafka AND Snowflake must not return Kafka-only companies."""
    body = build_query(q="x", technologies=["Kafka", "Snowflake"])
    terms = [f for f in _filters(body) if "enrichment.technologies" in str(f)]
    assert len(terms) == 2


def test_employee_range_produces_one_bounded_range_filter():
    body = build_query(q="x", min_employees=50, max_employees=500)
    ranges = [f for f in _filters(body) if "range" in f]
    assert ranges == [{"range": {"employee_count": {"gte": 50, "lte": 500}}}]


def test_empty_query_skips_function_score():
    """No text to score means no reason to pay for function_score."""
    body = build_query()
    assert "function_score" not in body["query"]
    assert "match_all" in body["query"]["bool"]["must"][0]


def test_fuzziness_protects_the_first_two_characters():
    mm = build_query(q="acme")["query"]["function_score"]["query"]["bool"]["must"][0]["multi_match"]
    assert mm["fuzziness"] == "AUTO"
    assert mm["prefix_length"] == 2


def test_query_requests_facets_and_total():
    body = build_query(q="x")
    assert body["track_total_hits"] is True
    assert set(body["aggs"]) >= {"by_city", "by_industry", "employee_bands"}


def test_suggest_uses_the_ngram_subfield_with_and_semantics():
    body = build_suggest_query("nor")
    match = body["query"]["bool"]["must"][0]["match"]["name.ngram"]
    assert match["query"] == "nor"
    assert match["operator"] == "and"


def test_mapping_is_strict():
    """Dynamic mapping is how a stray field silently breaks range queries."""
    assert MAPPINGS["dynamic"] == "strict"


def test_name_is_indexed_three_ways():
    name = MAPPINGS["properties"]["name"]
    assert name["analyzer"] == "company_name_analyzer"
    assert name["fields"]["keyword"]["type"] == "keyword"
    assert name["fields"]["ngram"]["analyzer"] == "company_name_ngram"
    # Search analyzer must NOT n-gram the query, or "acme" matches everything.
    assert name["fields"]["ngram"]["search_analyzer"] == "company_name_ngram_search"


def test_ngram_search_analyzer_has_no_ngram_filter():
    search = ANALYSIS["analyzer"]["company_name_ngram_search"]
    assert "edge_ngrams" not in search["filter"]


def test_legal_suffixes_are_stopped_at_analysis_time():
    stopwords = ANALYSIS["filter"]["company_suffixes"]["stopwords"]
    assert {"inc", "llc", "ltd", "corp"} <= set(stopwords)


def test_physical_index_name_is_versioned_and_timestamped():
    name = physical_index_name("companies", version=3)
    assert name.startswith("companies-v3-")
    assert len(name) > len("companies-v3-")


def test_physical_index_names_never_collide():
    """Second-granularity timestamps alone collide.

    A bootstrap followed immediately by a reindex would otherwise generate the
    same name, `create_index` would no-op, and the reindex would try to read
    and write one index -- which the server rejects.
    """
    names = {physical_index_name("companies") for _ in range(100)}
    assert len(names) == 100
