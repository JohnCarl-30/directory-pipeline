"""DOM extraction against the real fixture pages -- no network, no model."""

from __future__ import annotations

import pytest

from directory_pipeline.domain.models import ExtractionMethod, RawListing
from directory_pipeline.extraction.agent import (
    DomExtractor,
    ExtractionError,
    Extractor,
    TextFallbackExtractor,
)
from directory_pipeline.fixtures.data import BY_SLUG
from directory_pipeline.fixtures.mock_directory import (
    _drifted_page,
    _microdata_page,
    _stub_page,
)


def listing_for(slug: str, renderer) -> RawListing:
    company = BY_SLUG[slug]
    return RawListing(
        source="test",
        source_id=slug,
        url=f"http://directory.test/company/{slug}",
        html=renderer(company),
    )


async def test_microdata_page_extracts_every_field(settings):
    record = await Extractor(settings).extract(listing_for("northwind-analytics", _microdata_page))

    assert record.name == "Northwind Analytics, Inc."
    assert record.name_normalized == "northwind analytics"
    assert record.address.city == "Austin"
    assert record.address.region == "TX"  # "Texas" normalized
    assert record.address.postal_code == "78701"
    assert record.contact.phone_e164 == "+15125550142"
    assert record.contact.website == "https://northwindanalytics.com"
    assert record.contact.email == "hello@northwindanalytics.com"
    assert record.employee_count == 51  # low end of "51-200"
    assert record.founded_year == 2014
    assert "Analytics" in record.categories
    assert record.extraction_method is ExtractionMethod.DOM
    assert record.extraction_confidence == 1.0


async def test_related_listings_do_not_leak_into_the_record(settings):
    """The page links to Harbor Point Labs in an aside. It must not win."""
    record = await Extractor(settings).extract(listing_for("northwind-analytics", _microdata_page))
    assert "Harbor" not in record.name


async def test_drifted_template_is_recovered_by_the_text_layer(settings):
    """No microdata at all -- the facts are only in prose. Regex must find them.

    This is the case that would otherwise go to the model on every single page.
    """
    record = await Extractor(settings).extract(
        listing_for("northwind-analytics-llc", _drifted_page)
    )

    assert record.name == "Northwind Analytics LLC"
    assert record.extraction_method is ExtractionMethod.TEXT
    assert record.address.city == "Austin"
    assert record.address.region == "TX"
    assert record.address.postal_code == "78701"
    assert record.contact.phone_e164 == "+15125550142"
    assert record.contact.email == "sales@northwindanalytics.com"
    assert record.contact.website == "https://northwindanalytics.com"
    assert record.founded_year == 2014
    # An inference is never scored as certain, however complete it looks.
    assert record.extraction_confidence <= 0.9


async def test_text_layer_does_not_override_a_selector_hit(settings):
    """DOM is ground truth. The cheaper layer only fills holes."""
    record = await Extractor(settings).extract(listing_for("northwind-analytics", _microdata_page))
    assert record.extraction_method is ExtractionMethod.DOM
    assert record.contact.phone_e164 == "+15125550142"


def test_text_layer_ignores_navigation_and_related_listings():
    """A 'Related listings' aside is full of other companies' details."""
    html = """<html><body>
      <nav><a href="/">Home</a> 555-111-2222</nav>
      <article><h1>Real Co</h1>
        <p>Call us on (512) 555-0142 in Austin, TX 78701.</p></article>
      <aside>Related: Other Co, (999) 555-0000, Boston, MA 02210</aside>
      <footer>support@directory.test</footer>
    </body></html>"""
    listing = RawListing(
        source="t", source_id="1", url="http://directory.test/company/1", html=html
    )
    found = TextFallbackExtractor().extract(listing)

    assert found["phone"] == "(512) 555-0142"
    assert found["city"] == "Austin"
    assert found["region"] == "TX"
    assert "email" not in found  # the footer address was stripped


def test_text_layer_does_not_mistake_an_email_domain_for_a_website():
    html = "<html><body><p>Contact hello@acme.com for details.</p></body></html>"
    listing = RawListing(source="t", source_id="1", url="http://directory.test/c/1", html=html)
    found = TextFallbackExtractor().extract(listing)

    assert found["email"] == "hello@acme.com"
    assert "website" not in found


def test_text_layer_skips_the_directorys_own_host_and_social_links():
    html = """<html><body><p>Profile on directory.test. Follow us on linkedin.com/company/x.
    Our site is realcompany.io.</p></body></html>"""
    listing = RawListing(source="t", source_id="1", url="http://directory.test/c/1", html=html)
    assert TextFallbackExtractor().extract(listing)["website"] == "realcompany.io"


def test_text_layer_returns_nothing_for_a_page_with_no_facts():
    """Firing on the wrong thing is worse than not firing."""
    html = "<html><body><h1>Some Co</h1><p>This listing has not been claimed.</p></body></html>"
    listing = RawListing(source="t", source_id="1", url="http://directory.test/c/1", html=html)
    found = TextFallbackExtractor().extract(listing)

    assert "phone" not in found
    assert "city" not in found
    assert "website" not in found


async def test_stub_page_yields_a_sparse_low_confidence_record(settings):
    record = await Extractor(settings).extract(listing_for("silverpine-holdings", _stub_page))
    assert record.name == "Silverpine Holdings"
    assert record.contact.phone_e164 is None
    assert record.extraction_confidence <= 0.25


async def test_page_without_a_name_raises_extraction_error(settings):
    listing = RawListing(
        source="test",
        source_id="empty",
        url="http://x/empty",
        html="<html><body><p>nothing here</p></body></html>",
    )
    with pytest.raises(ExtractionError):
        await Extractor(settings).extract(listing)


async def test_record_id_is_deterministic(settings):
    extractor = Extractor(settings)
    first = await extractor.extract(listing_for("atlas-robotics", _microdata_page))
    second = await extractor.extract(listing_for("atlas-robotics", _microdata_page))
    assert first.record_id == second.record_id


def test_content_hash_detects_change():
    a = RawListing(source="t", source_id="1", url="u", html="<p>a</p>")
    b = RawListing(source="t", source_id="1", url="u", html="<p>a</p>")
    c = RawListing(source="t", source_id="1", url="u", html="<p>b</p>")
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_dom_extractor_prefers_href_over_link_text():
    """'Visit site' is useless; the href is the value."""
    html = '<a class="website" itemprop="url" href="https://real.example.com">Visit site</a>'
    listing = RawListing(source="t", source_id="1", url="u", html=html)
    assert DomExtractor().extract(listing)["website"] == "https://real.example.com"


async def test_document_shape_is_stable(settings):
    """to_document() is the OpenSearch contract -- mapping is `dynamic: strict`."""
    from directory_pipeline.domain.models import EnrichedCompany
    from directory_pipeline.search.index import MAPPINGS

    record = await Extractor(settings).extract(listing_for("atlas-robotics", _microdata_page))
    document = EnrichedCompany(company=record).to_document()

    allowed = set(MAPPINGS["properties"])
    unmapped = set(document) - allowed
    assert not unmapped, f"fields absent from a strict mapping would 400: {unmapped}"
