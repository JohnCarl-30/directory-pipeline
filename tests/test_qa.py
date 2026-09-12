"""QA reporting -- the checks that gate a release."""

from __future__ import annotations

from directory_pipeline.domain.models import (
    Address,
    CompanyRecord,
    Contact,
    EnrichedCompany,
    Enrichment,
)
from directory_pipeline.reporting import qa


def company(
    name: str,
    *,
    sid: str,
    phone=None,
    website=None,
    city=None,
    region=None,
    postal=None,
    duplicate_of=None,
    cluster="c1",
) -> EnrichedCompany:
    return EnrichedCompany(
        company=CompanyRecord(
            record_id=CompanyRecord.make_record_id("t", sid),
            source="t",
            source_id=sid,
            source_url=f"http://t/{sid}",
            name=name,
            name_normalized=name.lower(),
            address=Address(city=city, region=region, postal_code=postal),
            contact=Contact(phone_e164=phone, website=website),
        ),
        enrichment=Enrichment(provider="p", industry="Software", confidence=0.9),
        cluster_id=cluster,
        duplicate_of=duplicate_of,
    )


def test_empty_input_does_not_explode():
    frame = qa.to_frame([])
    assert frame.empty
    assert qa.summary(frame) == {"records": 0}
    assert qa.coverage(frame).empty


def test_coverage_flags_fields_below_expectation():
    rows = [company(f"Co {i}", sid=str(i), city="Austin") for i in range(10)]
    coverage = qa.coverage(qa.to_frame(rows))
    phone = coverage.set_index("field").loc["phone_e164"]
    assert phone["coverage"] == 0.0
    assert bool(phone["pass"]) is False


def test_validity_catches_malformed_normalized_values():
    good = company("Good", sid="1", phone="+15125550142", website="https://good.com")
    bad = company("Bad", sid="2", phone="5125550142", website="http://bad.com")
    checks = qa.validity(qa.to_frame([good, bad])).set_index("check")

    assert checks.loc["phone not E.164", "violations"] == 1
    assert checks.loc["website not https origin", "violations"] == 1


def test_duplicate_summary_lists_collapsed_members():
    canonical = company("Acme Inc", sid="1", cluster="k1")
    duplicate = company("Acme LLC", sid="2", cluster="k1", duplicate_of=canonical.company.record_id)
    summary = qa.duplicate_summary(qa.to_frame([canonical, duplicate]))

    assert len(summary) == 1
    assert summary.iloc[0]["members"] == 2
    assert summary.iloc[0]["canonical"] == "Acme Inc"
    assert "Acme LLC" in summary.iloc[0]["collapsed"]


def test_summary_quality_gate_fails_on_coverage_gaps():
    rows = [company(f"Co {i}", sid=str(i)) for i in range(5)]
    summary = qa.summary(qa.to_frame(rows))

    assert summary["records"] == 5
    assert summary["quality_gate"] == "FAIL"
    assert "phone_e164" in summary["coverage_failures"]


def test_summary_counts_canonicals_and_duplicates():
    canonical = company(
        "A",
        sid="1",
        phone="+15125550142",
        website="https://a.com",
        city="Austin",
        region="TX",
        postal="78701",
    )
    duplicate = company(
        "A Inc",
        sid="2",
        phone="+15125550142",
        website="https://a.com",
        city="Austin",
        region="TX",
        postal="78701",
        duplicate_of=canonical.company.record_id,
    )
    summary = qa.summary(qa.to_frame([canonical, duplicate]))

    assert summary["canonical_records"] == 1
    assert summary["duplicates_collapsed"] == 1
    assert summary["enrichment_rate"] == 1.0


def test_write_reports_creates_files(tmp_path):
    rows = [company("A", sid="1", phone="+15125550142", website="https://a.com")]
    written = qa.write_reports(qa.to_frame(rows), tmp_path)

    assert "records" in written
    assert (tmp_path / "records.csv").exists()
    assert (tmp_path / "coverage.csv").exists()
