"""Contracts that cross every boundary in the pipeline.

These models are the stable interface: the crawler produces `RawListing`, the
extractor turns that into `CompanyRecord`, enrichment attaches `Enrichment`, and
the indexer flattens the result into an OpenSearch document. Because they cross
Temporal activity boundaries they must stay JSON-serializable and
backward-compatible -- add optional fields, never repurpose existing ones.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 3


def _now() -> datetime:
    return datetime.now(UTC)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExtractionMethod(StrEnum):
    """How a record's fields were obtained, cheapest and most reliable first."""

    DOM = "dom"  # CSS/microdata selectors
    TEXT = "text"  # regex over visible prose
    LLM = "llm"  # model extraction
    LLM_REPAIRED = "llm_repaired"  # selectors, with model filling the gaps


class RawListing(BaseModel):
    """A fetched page plus the provenance needed to re-fetch or debug it."""

    model_config = ConfigDict(extra="forbid")

    source: str
    source_id: str
    url: str
    html: str
    fetched_at: datetime = Field(default_factory=_now)
    http_status: int = 200
    via_proxy: str | None = None

    @property
    def content_hash(self) -> str:
        """Stable hash of the body, used to skip unchanged pages on re-crawl."""
        return hashlib.sha256(self.html.encode("utf-8")).hexdigest()[:16]


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str = "US"

    def as_text(self) -> str:
        parts = [self.line1, self.line2, self.city, self.region, self.postal_code]
        return ", ".join(p for p in parts if p)


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_e164: str | None = None
    phone_raw: str | None = None
    email: str | None = None
    website: str | None = None


class CompanyRecord(BaseModel):
    """A normalized company, pre-enrichment."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    source: str
    source_id: str
    source_url: str

    name: str
    legal_name: str | None = None
    name_normalized: str = ""
    categories: list[str] = Field(default_factory=list)
    description: str | None = None

    address: Address = Field(default_factory=Address)
    contact: Contact = Field(default_factory=Contact)

    employee_count: int | None = None
    founded_year: int | None = None

    extraction_method: ExtractionMethod = ExtractionMethod.DOM
    extraction_confidence: float = 1.0
    content_hash: str = ""
    first_seen_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime = Field(default_factory=_now)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("company name is required")
        return v.strip()

    @staticmethod
    def make_record_id(source: str, source_id: str) -> str:
        """Deterministic id -> re-running the pipeline overwrites, never duplicates."""
        return hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()


class Enrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_company_id: str | None = None
    industry: str | None = None
    naics: str | None = None
    revenue_usd: int | None = None
    employee_count: int | None = None
    linkedin_url: str | None = None
    technologies: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    fetched_at: datetime = Field(default_factory=_now)
    cache_hit: bool = False


class EnrichedCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: CompanyRecord
    enrichment: Enrichment | None = None
    cluster_id: str | None = None
    duplicate_of: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_document(self) -> dict[str, Any]:
        """Flatten to the OpenSearch document shape defined in search.index."""
        c = self.company
        e = self.enrichment
        doc: dict[str, Any] = {
            "record_id": c.record_id,
            "schema_version": self.schema_version,
            "source": c.source,
            "source_id": c.source_id,
            "source_url": c.source_url,
            "name": c.name,
            "name_normalized": c.name_normalized,
            "legal_name": c.legal_name,
            "categories": c.categories,
            "description": c.description,
            "address": {
                "line1": c.address.line1,
                "city": c.address.city,
                "region": c.address.region,
                "postal_code": c.address.postal_code,
                "country": c.address.country,
                "full": c.address.as_text(),
            },
            "contact": {
                "phone_e164": c.contact.phone_e164,
                "email": c.contact.email,
                "website": c.contact.website,
            },
            "employee_count": e.employee_count if e and e.employee_count else c.employee_count,
            "founded_year": c.founded_year,
            "cluster_id": self.cluster_id,
            "duplicate_of": self.duplicate_of,
            "is_canonical": self.duplicate_of is None,
            "extraction": {
                "method": c.extraction_method.value,
                "confidence": round(c.extraction_confidence, 4),
            },
            "first_seen_at": c.first_seen_at.isoformat(),
            "last_seen_at": c.last_seen_at.isoformat(),
        }
        if e:
            doc["enrichment"] = {
                "provider": e.provider,
                "industry": e.industry,
                "naics": e.naics,
                "revenue_usd": e.revenue_usd,
                "linkedin_url": e.linkedin_url,
                "technologies": e.technologies,
                "confidence": round(e.confidence, 4),
                "fetched_at": e.fetched_at.isoformat(),
            }
        # Field used for relevance boosting -- richer records rank higher.
        doc["completeness"] = _completeness(doc)
        return doc


def _completeness(doc: dict[str, Any]) -> float:
    checks = [
        bool(doc.get("description")),
        bool(doc["address"].get("city")),
        bool(doc["address"].get("postal_code")),
        bool(doc["contact"].get("phone_e164")),
        bool(doc["contact"].get("website")),
        bool(doc.get("categories")),
        bool(doc.get("enrichment")),
        bool(doc.get("employee_count")),
    ]
    return round(sum(checks) / len(checks), 4)


# --- Workflow I/O -----------------------------------------------------------
# Temporal arguments are versioned contracts of their own: a running workflow
# deserializes these on replay, so they get the same add-only discipline.


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "demo-directory"
    categories: list[str] = Field(default_factory=lambda: ["software"])
    max_pages: int = 5
    batch_size: int = 25
    enrich: bool = True
    index_alias: str | None = None
    force_refetch: bool = False

    # Continue-as-new state. A crawl that exceeds the per-run URL budget hands
    # the remainder to a fresh run; without these two fields that work would be
    # silently dropped and the final counts would only cover the last run.
    pending_urls: list[str] = Field(default_factory=list)
    carried_totals: dict[str, int] = Field(default_factory=dict)


class CrawlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    pages_crawled: int = 0
    listings_found: int = 0
    records_extracted: int = 0
    records_enriched: int = 0
    duplicates_collapsed: int = 0
    documents_indexed: int = 0
    failures: list[str] = Field(default_factory=list)
    index_name: str | None = None
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


class ReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    reason: str = "mapping change"
    target_schema_version: int = SCHEMA_VERSION
    wait_for_completion: bool = False
    drop_old_index: bool = False


class ReindexResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    source_index: str | None
    target_index: str
    documents_copied: int
    swapped: bool
    old_index_dropped: bool = False
    duration_s: float = 0.0
