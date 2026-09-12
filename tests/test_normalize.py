"""Normalization is where most data quality comes from, so it gets the most tests."""

from __future__ import annotations

import pytest

from directory_pipeline.extraction import normalize as nz


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The Acme Corporation, Inc.", "acme"),
        ("Acme Corp", "acme"),
        ("ACME LLC", "acme"),
        ("Acme & Sons Ltd", "acme and sons"),
        ("Café Ünicode GmbH", "cafe unicode"),
        ("Northwind Analytics, Inc.", "northwind analytics"),
        ("Northwind Analytics LLC", "northwind analytics"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_company_name(raw, expected):
    assert nz.normalize_company_name(raw) == expected


def test_legal_suffix_variants_collapse_to_same_key():
    """The whole point: these must block together during entity resolution."""
    variants = ["Atlas Robotics", "Atlas Robotics, Inc.", "ATLAS ROBOTICS LLC"]
    keys = {nz.normalize_company_name(v) for v in variants}
    assert keys == {"atlas robotics"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(512) 555-0142", "+15125550142"),
        ("512-555-0142", "+15125550142"),
        ("+1 512 555 0142", "+15125550142"),
        ("15125550142", "+15125550142"),
        ("(503) 555-0110 ext. 4", "+15035550110"),  # extension dropped
        ("503.555.0110 x22", "+15035550110"),
        ("+44 20 7946 0958", "+442079460958"),
        ("not a phone", None),
        ("123", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert nz.normalize_phone(raw) == expected


def test_phone_formats_converge():
    forms = ["(206) 555-0164", "206-555-0164", "+12065550164", "206.555.0164"]
    assert len({nz.normalize_phone(f) for f in forms}) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.example.com/", "https://example.com"),
        ("example.com", "https://example.com"),
        ("http://Example.com/path/", "https://example.com/path"),
        ("https://example.com/x?utm_source=a", "https://example.com/x"),
        ("notaurl", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_url(raw, expected):
    assert nz.normalize_url(raw) == expected


def test_domain_of_ignores_www_and_scheme():
    assert nz.domain_of("http://www.Example.com/a") == "example.com"
    assert nz.domain_of("https://example.com") == "example.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Texas", "TX"),
        ("TX", "TX"),
        ("tx", "TX"),
        ("new york", "NY"),
        ("N.Y.", None),
        ("Nowhere", None),
        (None, None),
    ],
)
def test_normalize_region(raw, expected):
    assert nz.normalize_region(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("78701", "78701"),
        ("78701-1234", "78701"),
        ("Austin, TX 78701", "78701"),
        ("no zip", None),
        (None, None),
    ],
)
def test_normalize_postal_code(raw, expected):
    assert nz.normalize_postal_code(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("51-200 employees", 51),
        ("~500", 500),
        ("1,200 staff", 1200),
        ("2k employees", 2000),
        ("none", None),
        (None, None),
    ],
)
def test_parse_employee_count(raw, expected):
    """Ranges collapse to the low end -- understating beats overstating."""
    assert nz.parse_employee_count(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Founded 2014", 2014), ("2014", 2014), ("est. 1987", 1987), ("nope", None)],
)
def test_parse_year(raw, expected):
    assert nz.parse_year(raw) == expected


def test_normalize_email():
    assert nz.normalize_email("mailto:A@B.COM") == "a@b.com"
    assert nz.normalize_email("not-an-email") is None


def test_split_categories_dedupes():
    assert nz.split_categories("Analytics, Retail | analytics") == ["Analytics", "Retail"]


def test_all_normalizers_are_total():
    """Bad input must never raise -- one malformed field cannot fail a batch."""
    junk = ["", "   ", "\x00", "🙂" * 50, "-" * 500, "NULL", "N/A"]
    for value in junk:
        nz.clean_text(value)
        nz.normalize_company_name(value)
        nz.normalize_phone(value)
        nz.normalize_url(value)
        nz.normalize_email(value)
        nz.normalize_region(value)
        nz.normalize_postal_code(value)
        nz.parse_employee_count(value)
        nz.parse_year(value)
        nz.split_categories(value)
