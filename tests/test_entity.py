"""Entity resolution: blocking recall, scoring calibration, clustering."""

from __future__ import annotations

from directory_pipeline.domain.models import Address, CompanyRecord, Contact
from directory_pipeline.extraction.normalize import normalize_company_name
from directory_pipeline.resolution.entity import (
    blocking_keys,
    generate_candidates,
    jaro_winkler,
    resolve,
    score_pair,
)


def make(
    name: str,
    *,
    source_id: str | None = None,
    website: str | None = None,
    phone: str | None = None,
    city: str | None = None,
    region: str | None = None,
    postal: str | None = None,
    email: str | None = None,
) -> CompanyRecord:
    sid = source_id or name.lower().replace(" ", "-").replace(",", "").replace(".", "")
    return CompanyRecord(
        record_id=CompanyRecord.make_record_id("test", sid),
        source="test",
        source_id=sid,
        source_url=f"http://test/{sid}",
        name=name,
        name_normalized=normalize_company_name(name),
        address=Address(city=city, region=region, postal_code=postal),
        contact=Contact(website=website, phone_e164=phone, email=email),
    )


def test_jaro_winkler_rewards_shared_prefix():
    assert jaro_winkler("acme systems", "acme systems") == 1.0
    assert jaro_winkler("acme systems", "acme system") > 0.95
    assert jaro_winkler("acme", "zebra") < 0.5


def test_blocking_produces_multiple_independent_keys():
    """Recall depends on this: a record missing one signal is caught by another."""
    record = make(
        "Acme Systems",
        website="https://acme.com",
        phone="+15125550142",
        city="Austin",
        email="a@acme.com",
    )
    keys = blocking_keys(record)
    assert "dom:acme.com" in keys
    assert "tel:+15125550142" in keys
    assert "eml:a@acme.com" in keys
    assert any(k.startswith("nm:acme:") for k in keys)
    assert len(keys) >= 4


def test_same_domain_scores_as_match():
    a = make(
        "Northwind Analytics, Inc.",
        source_id="a",
        website="https://northwind.com",
        city="Austin",
        region="TX",
    )
    b = make(
        "Northwind Analytics LLC",
        source_id="b",
        website="http://www.northwind.com/",
        city="Austin",
        region="TX",
    )
    candidate = score_pair(a, b)
    assert candidate.is_match, candidate.signals


def test_different_domains_score_as_distinct():
    """Domain disagreement is evidence *against*, not just absence of evidence."""
    a = make(
        "Acme Systems", source_id="a", website="https://acme-a.com", city="Austin", region="TX"
    )
    b = make(
        "Acme Systems", source_id="b", website="https://acme-b.com", city="Austin", region="TX"
    )
    candidate = score_pair(a, b)
    assert not candidate.is_match, candidate.signals


def test_franchise_same_brand_different_city_is_not_a_match():
    a = make(
        "Cascade Freight Systems",
        source_id="pdx",
        phone="+15035550110",
        city="Portland",
        region="OR",
    )
    b = make(
        "Cascade Freight Systems",
        source_id="sea",
        phone="+12065550188",
        city="Seattle",
        region="WA",
    )
    assert not score_pair(a, b).is_match


def test_shared_domain_across_cities_is_a_branch_not_a_duplicate():
    """Every location of a chain lists the same corporate website.

    Domain agreement alone would merge a whole multi-site company into one
    record. It must land in the review band instead, for the adjudicator.
    """
    pdx = make(
        "Cascade Freight Systems Corp.",
        source_id="pdx",
        website="https://cascadefreight.com",
        city="Portland",
        region="OR",
    )
    sea = make(
        "Cascade Freight Systems",
        source_id="sea",
        website="https://cascadefreight.com",
        city="Seattle",
        region="WA",
    )

    candidate = score_pair(pdx, sea)
    assert not candidate.is_match, candidate.signals
    assert candidate.needs_review, candidate.score
    assert candidate.signals.get("location_conflict") == 1.0


def test_shared_domain_in_the_same_city_still_matches():
    """The conflict penalty must not break the ordinary duplicate case."""
    a = make(
        "Northwind Analytics, Inc.",
        source_id="a",
        website="https://northwind.com",
        city="Austin",
        region="TX",
    )
    b = make(
        "Northwind Analytics LLC",
        source_id="b",
        website="https://northwind.com",
        city="Austin",
        region="TX",
    )

    candidate = score_pair(a, b)
    assert candidate.is_match
    assert "location_conflict" not in candidate.signals


def test_missing_address_is_not_treated_as_a_location_conflict():
    """Absence of an address is not evidence of a different one."""
    known = make(
        "Atlas Robotics", source_id="a", website="https://atlas.ai", city="Seattle", region="WA"
    )
    sparse = make("Atlas Robotics Inc", source_id="b", website="https://atlas.ai")

    candidate = score_pair(known, sparse)
    assert candidate.is_match
    assert "location_conflict" not in candidate.signals


def test_shared_phone_and_name_matches_without_a_website():
    a = make("Quarry Lane Foods", source_id="a", phone="+16145550155", city="Columbus", region="OH")
    b = make(
        "Quarry Lane Foods Inc", source_id="b", phone="+16145550155", city="Columbus", region="OH"
    )
    assert score_pair(a, b).is_match


def test_generate_candidates_skips_unrelated_pairs():
    records = [
        make("Acme Systems", source_id="1", website="https://acme.com"),
        make("Acme Systems Inc", source_id="2", website="https://acme.com"),
        make("Totally Different Co", source_id="3", website="https://other.com"),
    ]
    pairs = generate_candidates(records)
    names = {(c.left.source_id, c.right.source_id) for c in pairs}
    assert ("1", "2") in names or ("2", "1") in names
    assert all("3" not in pair for pair in names)


def test_resolve_clusters_and_picks_the_most_complete_canonical():
    sparse = make("Atlas Robotics", source_id="sparse", website="https://atlas.ai")
    rich = make(
        "Atlas Robotics, Inc.",
        source_id="rich",
        website="https://atlas.ai",
        phone="+12065550164",
        city="Seattle",
        region="WA",
        postal="98103",
        email="hi@atlas.ai",
    )
    cluster_of, canonical_of = resolve([sparse, rich])

    assert cluster_of[sparse.record_id] == cluster_of[rich.record_id]
    # The richer record survives as canonical.
    assert canonical_of[sparse.record_id] == rich.record_id
    assert canonical_of[rich.record_id] == rich.record_id


def test_resolve_is_deterministic_across_input_order():
    """Re-running the pipeline must pick the same canonical record every time."""
    a = make("Acme Systems", source_id="a", website="https://acme.com", city="Austin")
    b = make("Acme Systems Inc", source_id="b", website="https://acme.com", city="Austin")
    forward = resolve([a, b])[1]
    backward = resolve([b, a])[1]
    assert forward == backward


def test_resolve_is_transitive():
    """A~B and B~C must put all three in one cluster."""
    a = make("Vantage Grid", source_id="a", website="https://vantage.com")
    b = make(
        "Vantage Grid Energy", source_id="b", website="https://vantage.com", phone="+17205550133"
    )
    c = make("Vantage Grid Energy Inc", source_id="c", phone="+17205550133")
    cluster_of, _ = resolve([a, b, c])
    assert len({cluster_of[x.record_id] for x in (a, b, c)}) == 1


def test_singletons_are_their_own_cluster_and_canonical():
    lone = make("Solo Co", source_id="solo", website="https://solo.com")
    cluster_of, canonical_of = resolve([lone])
    assert cluster_of[lone.record_id] == lone.record_id
    assert canonical_of[lone.record_id] == lone.record_id
