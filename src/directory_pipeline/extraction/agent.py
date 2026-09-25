"""Extraction: deterministic DOM first, agentic LLM only where it pays.

The design point is cost, not capability. A directory page is 95% boilerplate;
CSS selectors extract it for free and never hallucinate. The LLM earns its cost
on the remaining 5% -- pages where the template drifted, fields are collapsed
into prose, or a selector silently returns nothing.

So the flow is three layers, cheapest first:

    1. DOM selectors   free, exact, cannot hallucinate
    2. Text patterns   free, regex over the page's visible prose -- catches the
                       common case of a drifted template that still writes a
                       phone number in a sentence
    3. LLM extraction  costs money and latency; only reached when 1 and 2 leave
                       a critical field missing

Skipping layer 2 would send every template change straight to the model, which
is the expensive way to solve a problem regex already solves. Each layer only
fills fields the layer above it missed: a selector hit is ground truth, a regex
hit is a strong inference, a model output is a weaker one. Nothing ever
overwrites a more reliable source.

The LLM call uses structured outputs (`output_config.format`), so the response
is schema-valid JSON by construction -- no regex, no repair loop, no
`json.loads` in a try/except. The system prompt is cached, since it is identical
across every page in a crawl.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from ..config import Settings
from ..domain.models import (
    Address,
    CompanyRecord,
    Contact,
    ExtractionMethod,
    RawListing,
)
from ..observability import METRICS, get_logger, timed
from . import normalize as nz

log = get_logger(__name__)

# Structured-output schema. `additionalProperties: false` plus an explicit
# `required` list is what makes the output a guarantee rather than a hope --
# every field is present, nullable rather than absent, so downstream code never
# branches on key existence.
COMPANY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Trading name of the company."},
        "legal_name": {
            "type": ["string", "null"],
            "description": "Registered legal entity name if stated separately.",
        },
        "description": {
            "type": ["string", "null"],
            "description": "One or two sentences describing what the company does.",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Industry or service categories listed on the page.",
        },
        "address_line1": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "region": {
            "type": ["string", "null"],
            "description": "State or province, as written on the page.",
        },
        "postal_code": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"], "description": "ISO-3166 alpha-2 if determinable."},
        "phone": {"type": ["string", "null"], "description": "Primary phone, as written."},
        "email": {"type": ["string", "null"]},
        "website": {
            "type": ["string", "null"],
            "description": "Company's own website, not the directory's.",
        },
        "employee_count": {
            "type": ["string", "null"],
            "description": "Headcount as written, e.g. '51-200 employees'. Do not convert.",
        },
        "founded_year": {"type": ["string", "null"]},
        "confidence": {
            "type": "number",
            "description": (
                "0.0-1.0. How confident you are that these fields describe one "
                "company and were actually present on the page."
            ),
        },
    },
    "required": [
        "name",
        "legal_name",
        "description",
        "categories",
        "address_line1",
        "city",
        "region",
        "postal_code",
        "country",
        "phone",
        "email",
        "website",
        "employee_count",
        "founded_year",
        "confidence",
    ],
    "additionalProperties": False,
}

# Stable across every page in a crawl -> cacheable prefix. Nothing volatile
# (no timestamps, no page URL) may appear here or the cache never reads.
_SYSTEM_PROMPT = """\
You extract company records from business-directory pages.

Rules:
- Copy values as they appear on the page. Do not normalize, reformat, or expand \
abbreviations -- a downstream deterministic step does that and does it better.
- If a field is not present on the page, return null. Never infer a plausible \
value, and never carry a value over from an example.
- `website` is the company's own site. Directory-internal links, social profiles, \
and the directory's own domain are not the website.
- Ignore navigation, ads, cookie banners, "related listings", and any company \
other than the one the page is about.
- `confidence` reflects whether the page actually said these things. A page that \
is a stub, a search result, or an error page should score below 0.4.\
"""

_MAX_HTML_CHARS = 60_000  # ~15k tokens; directory detail pages are far smaller


class ExtractionError(RuntimeError):
    pass


class DomExtractor:
    """Selector-based extraction. Fast, free, and incapable of inventing data."""

    # Ordered fallbacks per field -- microdata first (most reliable), then the
    # demo directory's own classes, then generic conventions.
    SELECTORS: dict[str, tuple[str, ...]] = {
        "name": ('[itemprop="name"]', "h1.company-name", "h1"),
        "legal_name": ('[itemprop="legalName"]', ".legal-name"),
        "description": (
            '[itemprop="description"]',
            ".company-description",
            "meta[name=description]",
        ),
        "categories": ('[itemprop="category"]', ".category", ".tags .tag"),
        "address_line1": ('[itemprop="streetAddress"]', ".street-address"),
        "city": ('[itemprop="addressLocality"]', ".locality"),
        "region": ('[itemprop="addressRegion"]', ".region"),
        "postal_code": ('[itemprop="postalCode"]', ".postal-code"),
        "phone": ('[itemprop="telephone"]', ".phone", 'a[href^="tel:"]'),
        "email": ('[itemprop="email"]', 'a[href^="mailto:"]'),
        "website": ('[itemprop="url"]', "a.website", ".website a"),
        "employee_count": ('[itemprop="numberOfEmployees"]', ".employees"),
        "founded_year": ('[itemprop="foundingDate"]', ".founded"),
    }

    def extract(self, listing: RawListing) -> dict[str, Any]:
        tree = HTMLParser(listing.html)
        out: dict[str, Any] = {}
        for field, selectors in self.SELECTORS.items():
            value = self._first(tree, selectors, multi=(field == "categories"))
            if value:
                out[field] = value
        return out

    def _first(self, tree: HTMLParser, selectors: tuple[str, ...], *, multi: bool = False) -> Any:
        for selector in selectors:
            nodes = tree.css(selector)
            if not nodes:
                continue
            if multi:
                values = [self._value(n) for n in nodes]
                values = [v for v in values if v]
                if values:
                    return values
                continue
            value = self._value(nodes[0])
            if value:
                return value
        return [] if multi else None

    @staticmethod
    def _value(node: Any) -> str | None:
        """Prefer the attribute that carries the real value over display text."""
        for attr in ("content", "datetime"):
            if val := node.attributes.get(attr):
                return val.strip()
        href = node.attributes.get("href")
        if href:
            if href.startswith("mailto:"):
                return href[7:].strip()
            if href.startswith("tel:"):
                return href[4:].strip()
            if href.startswith(("http://", "https://")):
                # Only trust href as a value for link-shaped fields; the text of
                # an <a> is often "Visit site", which is useless.
                text = node.text(strip=True)
                return href.strip() if not text or len(text) < 30 else text
        return node.text(strip=True) or None


class TextFallbackExtractor:
    """Regex extraction over the page's visible prose.

    For pages where the template drifted and the selectors miss, but the facts
    are still written down in a sentence: "Reach the team on 512-555-0142".

    Navigation, asides and footers are stripped first -- a "Related listings"
    block is full of other companies' details, and picking one up here would be
    worse than extracting nothing.
    """

    # Deliberately conservative. A pattern that fires on the wrong thing is
    # worse than one that does not fire: a missing field is visible in the QA
    # coverage report, a wrong field silently corrupts entity resolution.
    PHONE_RE = re.compile(
        r"(?:\+1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?:\s*(?:ext|x)\.?\s*\d+)?",
        re.IGNORECASE,
    )
    # Label-by-label, so a sentence-ending full stop is not swallowed into the
    # TLD ("...@acme.com." must yield "@acme.com", not "@acme.com.").
    EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    DOMAIN_RE = re.compile(r"\b(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", re.I)
    # "Austin, TX 78701" and "Austin, TX" -- the anchor for the whole address.
    CITY_STATE_RE = re.compile(
        r"([A-Z][A-Za-z.'\- ]{1,40}?),\s*([A-Z]{2})\b(?:\s+(\d{5})(?:-\d{4})?)?"
    )
    STREET_RE = re.compile(
        r"(\d{1,6}\s+[A-Za-z0-9.'\- ]{3,60}?)(?=,\s*[A-Z][A-Za-z.'\- ]{1,40},\s*[A-Z]{2}\b)"
    )
    EMPLOYEES_RE = re.compile(
        r"(?:around|about|approximately|~)?\s*([\d,]+\s*(?:k\b)?(?:\s*[-\u2013]\s*[\d,]+)?)\s*"
        r"(?:employees|staff|people|headcount)",
        re.IGNORECASE,
    )
    SINCE_RE = re.compile(
        r"(?:since|founded|established|est\.?)\s*(?:in\s*)?(1[6-9]\d{2}|20\d{2})", re.I
    )
    # Hosts that are never the company's own website.
    HOST_DENYLIST = {
        "schema.org",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "google.com",
        "example.com",
    }

    # Placeholder prose on unclaimed listings. Emitting this as a description
    # would be worse than emitting nothing: it reads as real copy downstream.
    BOILERPLATE_RE = re.compile(
        r"(has not been claimed|claim this (?:business|listing)|no description"
        r"|description coming soon|under construction)",
        re.IGNORECASE,
    )
    # Long enough to be a sentence about the company, short enough not to be
    # the whole page flattened into one node.
    DESCRIPTION_MIN_CHARS = 40
    DESCRIPTION_MAX_CHARS = 600

    STRIP_SELECTORS = ("nav", "aside", "footer", "header", "script", "style", "noscript")

    def extract(self, listing: RawListing) -> dict[str, Any]:
        tree = HTMLParser(listing.html)
        for selector in self.STRIP_SELECTORS:
            for node in tree.css(selector):
                node.decompose()
        body = tree.body or tree.root
        if body is None:
            return {}
        text = " ".join(body.text(separator=" ", strip=True).split())
        if not text:
            return {}

        out: dict[str, Any] = {}

        # Description needs paragraph structure, so it is read before the body
        # is flattened. Contact prose ("Reach the team on 512-555-0142") lives
        # in its own paragraph on drifted templates, so a paragraph carrying a
        # phone, an email or a street address is skipped rather than guessed at.
        for node in body.css("p"):
            para = " ".join(node.text(separator=" ", strip=True).split())
            if not (self.DESCRIPTION_MIN_CHARS <= len(para) <= self.DESCRIPTION_MAX_CHARS):
                continue
            if self.BOILERPLATE_RE.search(para):
                continue
            if self.EMAIL_RE.search(para) or self.PHONE_RE.search(para):
                continue
            if self.STREET_RE.search(para) or self.CITY_STATE_RE.search(para):
                continue
            out["description"] = para
            break
        own_host = urlsplit(listing.url).netloc.lower().removeprefix("www.")

        if email := self.EMAIL_RE.search(text):
            out["email"] = email.group(0)

        # Remove emails before hunting for domains, or "a@acme.com" yields
        # "acme.com" as a website even when the site is never mentioned.
        domain_text = self.EMAIL_RE.sub(" ", text)
        for match in self.DOMAIN_RE.finditer(domain_text):
            host = match.group(1).lower().removeprefix("www.")
            if host == own_host or host in self.HOST_DENYLIST:
                continue
            if "." not in host or host.rsplit(".", 1)[-1].isdigit():
                continue
            out["website"] = host
            break

        if phone := self.PHONE_RE.search(text):
            out["phone"] = phone.group(0)

        if location := self.CITY_STATE_RE.search(text):
            out["city"] = location.group(1).strip()
            out["region"] = location.group(2)
            if location.group(3):
                out["postal_code"] = location.group(3)

        if street := self.STREET_RE.search(text):
            out["address_line1"] = street.group(1).strip()

        if employees := self.EMPLOYEES_RE.search(text):
            out["employee_count"] = employees.group(1).strip()

        if founded := self.SINCE_RE.search(text):
            out["founded_year"] = founded.group(1)

        return out


class LLMExtractor:
    """Agentic extraction with guaranteed-shape output.

    Structured outputs do the work a retry loop used to: the response is valid
    against COMPANY_SCHEMA or the request fails. What remains to handle is
    refusal (a 200 with no usable content) and transport failure, both of which
    degrade to "return nothing" so the DOM result still stands.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ExtractionError(
                    "anthropic package not installed; set EXTRACTION_MODE=dom"
                ) from exc
            self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key or None)
        return self._client

    async def extract(self, listing: RawListing, *, missing: list[str]) -> dict[str, Any]:
        client = self._get_client()
        html = listing.html[:_MAX_HTML_CHARS]

        # Volatile content (the page, the field list) goes AFTER the cache
        # breakpoint so the cached prefix stays byte-identical across pages.
        user_content = (
            f"Extract the company record from this page.\n"
            f"Fields the deterministic parser could not find: {', '.join(missing) or 'none'}.\n"
            f"Source URL: {listing.url}\n\n"
            f"<page>\n{html}\n</page>"
        )

        with timed("extract.llm"):
            response = await client.messages.create(
                model=self.settings.extraction_model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={
                    # Extraction is shallow work -- low effort keeps cost and
                    # latency down without measurably hurting field accuracy.
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": COMPANY_SCHEMA},
                },
                messages=[{"role": "user", "content": user_content}],
            )

        # A refusal is a successful HTTP 200 with no usable content. Reading
        # content[0] unconditionally is the classic way this breaks in prod.
        if response.stop_reason == "refusal":
            METRICS.incr("extract.llm_refusal", url=listing.url)
            log.warning("extract.llm_refusal", url=listing.url)
            return {}
        if response.stop_reason == "max_tokens":
            METRICS.incr("extract.llm_truncated")
            log.warning("extract.llm_truncated", url=listing.url)
            return {}

        usage = getattr(response, "usage", None)
        if usage is not None:
            METRICS.incr("extract.llm_input_tokens", float(usage.input_tokens or 0))
            METRICS.incr("extract.llm_output_tokens", float(usage.output_tokens or 0))
            METRICS.incr(
                "extract.llm_cache_read_tokens",
                float(getattr(usage, "cache_read_input_tokens", 0) or 0),
            )

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return {}
        return json.loads(text)  # schema-constrained: guaranteed parseable


class Extractor:
    """Facade: DOM extraction, optionally repaired by the LLM."""

    # Fields whose absence means the record is not worth indexing. If the DOM
    # pass misses one of these, the page is worth spending a model call on.
    CRITICAL_FIELDS = ("name", "city", "phone", "website")

    def __init__(self, settings: Settings, *, confidence_floor: float = 0.75) -> None:
        self.settings = settings
        self.dom = DomExtractor()
        self.text = TextFallbackExtractor()
        self.llm = LLMExtractor(settings) if settings.llm_extraction_enabled else None
        self.confidence_floor = confidence_floor

    async def extract(self, listing: RawListing) -> CompanyRecord:
        raw = self.dom.extract(listing)
        method = ExtractionMethod.DOM

        # Layer 2: fill only what the selectors missed. DOM values always win.
        if [f for f in self.CRITICAL_FIELDS if not raw.get(f)]:
            from_text = self.text.extract(listing)
            if from_text:
                filled = {k: v for k, v in from_text.items() if v and not raw.get(k)}
                if filled:
                    raw = {**raw, **filled}
                    method = ExtractionMethod.TEXT
                    METRICS.incr("extract.text_fallback_filled", len(filled))

        confidence = self._score(raw)
        missing = [f for f in self.CRITICAL_FIELDS if not raw.get(f)]
        if self.llm and (missing or confidence < self.confidence_floor):
            try:
                inferred = await self.llm.extract(listing, missing=missing)
            except Exception as exc:  # degrade, never fail the batch
                METRICS.incr("extract.llm_error", error=type(exc).__name__)
                log.warning("extract.llm_failed", url=listing.url, error=str(exc))
                inferred = {}
            if inferred:
                # DOM wins: a selector hit is evidence, a model field is a guess.
                merged = {**{k: v for k, v in inferred.items() if v not in (None, [], "")}, **raw}
                model_confidence = float(inferred.get("confidence") or 0.0)
                confidence = max(self._score(merged), min(model_confidence, 0.95))
                raw = merged
                method = ExtractionMethod.LLM_REPAIRED
                METRICS.incr("extract.llm_repaired")
        elif not self.llm and missing:
            METRICS.incr("extract.dom_incomplete", missing=",".join(missing))

        return self._to_record(listing, raw, method, min(confidence, self._cap_for(method)))

    def _score(self, raw: dict[str, Any]) -> float:
        present = sum(1 for f in self.CRITICAL_FIELDS if raw.get(f))
        return round(present / len(self.CRITICAL_FIELDS), 4)

    @staticmethod
    def _cap_for(method: ExtractionMethod) -> float:
        """Regex and model output are inferences; never score them as certain."""
        return {
            ExtractionMethod.DOM: 1.0,
            ExtractionMethod.TEXT: 0.9,
            ExtractionMethod.LLM: 0.85,
            ExtractionMethod.LLM_REPAIRED: 0.9,
        }[method]

    def _to_record(
        self,
        listing: RawListing,
        raw: dict[str, Any],
        method: ExtractionMethod,
        confidence: float,
    ) -> CompanyRecord:
        """Normalization happens here, in one place, for both extraction paths."""
        name = nz.clean_text(raw.get("name"))
        if not name:
            raise ExtractionError(f"no company name found at {listing.url}")

        categories = raw.get("categories") or []
        if isinstance(categories, str):
            categories = nz.split_categories(categories)
        else:
            categories = [c for c in (nz.clean_text(c) for c in categories) if c]

        return CompanyRecord(
            record_id=CompanyRecord.make_record_id(listing.source, listing.source_id),
            source=listing.source,
            source_id=listing.source_id,
            source_url=listing.url,
            name=name,
            legal_name=nz.clean_text(raw.get("legal_name")),
            name_normalized=nz.normalize_company_name(name),
            categories=categories,
            description=nz.clean_text(raw.get("description")),
            address=Address(
                line1=nz.clean_text(raw.get("address_line1")),
                city=nz.clean_text(raw.get("city")),
                region=nz.normalize_region(raw.get("region")),
                postal_code=nz.normalize_postal_code(raw.get("postal_code")),
                country=(nz.clean_text(raw.get("country")) or "US")[:2].upper(),
            ),
            contact=Contact(
                phone_e164=nz.normalize_phone(raw.get("phone")),
                phone_raw=nz.clean_text(raw.get("phone")),
                email=nz.normalize_email(raw.get("email")),
                website=nz.normalize_url(raw.get("website")),
            ),
            employee_count=nz.parse_employee_count(raw.get("employee_count")),
            founded_year=nz.parse_year(raw.get("founded_year")),
            extraction_method=method,
            extraction_confidence=confidence,
            content_hash=listing.content_hash,
        )
