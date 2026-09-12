"""Deterministic normalization.

This is where most data quality actually comes from. The LLM extractor is good
at finding fields in messy HTML; it is not the right tool for deciding that
"(415) 555-0142 ext. 2" and "+1 415-555-0142" are the same phone number. Keep
that judgment here, where it is testable and free.

Every function is total: bad input returns None rather than raising, because a
single malformed phone number must never fail a 10k-record batch.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

# Suffixes stripped before matching -- "Acme Inc." and "Acme LLC" should block
# together during entity resolution even though they are different legal forms.
_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "l l c",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "nv",
    "bv",
    "pty",
    "llp",
    "lp",
    "holdings",
    "group",
    "the",
}

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_YEAR = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b")

_US_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace, normalize unicode, strip. Returns None if empty."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).replace("\xa0", " ")
    text = _WS.sub(" ", text).strip()
    return text or None


def normalize_company_name(name: str | None) -> str:
    """Lowercase, strip punctuation and legal suffixes -> a blocking key.

    "The Acme Corporation, Inc." -> "acme"
    """
    cleaned = clean_text(name)
    if not cleaned:
        return ""
    folded = unicodedata.normalize("NFKD", cleaned.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.replace("&", " and ")
    folded = _PUNCT.sub(" ", folded)
    tokens = [t for t in _WS.sub(" ", folded).strip().split(" ") if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in _LEGAL_SUFFIXES:
        tokens.pop(0)
    return " ".join(tokens)


def normalize_phone(raw: str | None, default_country_code: str = "1") -> str | None:
    """Best-effort E.164. Drops extensions -- they are not part of the identity."""
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    # Cut anything after an extension marker before collecting digits.
    # `x22` has no word boundary between the x and the digits, so the marker
    # for a bare `x` is "x immediately preceding a number", not `\bx\b`.
    cleaned = re.split(r"(?i)\b(?:ext|extension)\b\.?|\bx\s*(?=\d)", cleaned)[0]
    has_plus = cleaned.strip().startswith("+")
    digits = "".join(_DIGITS.findall(cleaned))
    if not digits:
        return None
    if has_plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith(default_country_code):
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def normalize_url(raw: str | None) -> str | None:
    """Canonical https origin + path. Drops tracking params and trailing slash."""
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    if "." not in cleaned:
        return None
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return None
    if not parts.netloc:
        return None
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, "", ""))


def domain_of(url: str | None) -> str | None:
    """Registrable-ish domain, used as a high-precision entity resolution key."""
    normalized = normalize_url(url)
    if not normalized:
        return None
    return urlsplit(normalized).netloc or None


def normalize_email(raw: str | None) -> str | None:
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    candidate = cleaned.lower().removeprefix("mailto:").split("?")[0].strip()
    return candidate if _EMAIL.match(candidate) else None


def normalize_region(raw: str | None) -> str | None:
    """US state name or abbreviation -> two-letter code."""
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    candidate = cleaned.strip().rstrip(".")
    if len(candidate) == 2 and candidate.upper() in set(_US_STATES.values()):
        return candidate.upper()
    return _US_STATES.get(candidate.lower())


def normalize_postal_code(raw: str | None) -> str | None:
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    match = _ZIP.search(cleaned)
    return match.group(1) if match else None


def parse_employee_count(raw: str | None) -> int | None:
    """Handles "51-200 employees", "~500", "1,200 staff" -> an int.

    Ranges collapse to the low end: understating is safer than overstating for
    range filters, and the low end is the value the source actually asserted.
    """
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", cleaned)]
    if not numbers:
        return None
    value = numbers[0]
    if re.search(r"(?i)\d\s*k\b|\bthousand\b", cleaned) and value < 1000:
        value *= 1000
    return value if 0 < value < 10_000_000 else None


def parse_year(raw: str | None) -> int | None:
    cleaned = clean_text(raw)
    if not cleaned:
        return None
    match = _YEAR.search(cleaned)
    return int(match.group(1)) if match else None


def split_categories(raw: str | None) -> list[str]:
    """Split a delimited category blob into a deduped, title-cased list."""
    cleaned = clean_text(raw)
    if not cleaned:
        return []
    parts = re.split(r"[,/|;•·]| - ", cleaned)
    seen: dict[str, str] = {}
    for part in parts:
        item = clean_text(part)
        if not item or len(item) > 60:
            continue
        seen.setdefault(item.lower(), item.title() if item.islower() else item)
    return list(seen.values())
