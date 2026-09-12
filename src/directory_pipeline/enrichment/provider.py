"""Third-party enrichment with the four things that make fan-out safe.

1. Rate-limit awareness   -- shares the ResilientClient's token bucket.
2. Idempotency            -- every write-ish call carries a deterministic key,
                             so a retry after a timeout cannot double-charge.
3. Deduplication          -- concurrent lookups of the same key share one
                             in-flight request instead of racing.
4. Caching                -- a TTL cache keyed by domain, because enrichment
                             providers bill per lookup and companies do not
                             change industry between batches.

The cache here is in-process for demo purposes; the interface is the same one
you would put Redis behind (`get`/`set` with a TTL).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain.models import CompanyRecord, Enrichment
from ..extraction.normalize import domain_of
from ..observability import METRICS, get_logger, timed
from ..scraping.client import FetchError, ResilientClient

log = get_logger(__name__)


@dataclass
class _CacheEntry:
    value: dict[str, Any]
    expires_at: float


class TTLCache:
    def __init__(self, ttl_s: float = 3600.0, max_entries: int = 50_000) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._data: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._data[key]
            return None
        return entry.value

    def set(self, key: str, value: dict[str, Any]) -> None:
        if len(self._data) >= self.max_entries:
            # Cheap eviction: drop the oldest-expiring tenth.
            doomed = sorted(self._data, key=lambda k: self._data[k].expires_at)
            for k in doomed[: self.max_entries // 10]:
                del self._data[k]
        self._data[key] = _CacheEntry(value, time.monotonic() + self.ttl_s)


class EnrichmentProvider:
    """Client for the (mock) enrichment API."""

    def __init__(self, settings: Settings, client: ResilientClient | None = None) -> None:
        self.settings = settings
        self.client = client or ResilientClient(
            settings, rps=settings.enrich_rps, burst=max(1, int(settings.enrich_rps))
        )
        self._owns_client = client is None
        self.cache = TTLCache()
        self._semaphore = asyncio.Semaphore(settings.enrich_concurrency)
        # Single-flight: key -> the one task actually doing the work.
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def idempotency_key(company: CompanyRecord) -> str:
        """Deterministic per (record, schema) -- a retry reuses the same key."""
        material = f"{company.record_id}:{company.name_normalized}:v1"
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    def _cache_key(self, company: CompanyRecord) -> str:
        return domain_of(company.contact.website) or f"name:{company.name_normalized}"

    async def enrich(self, company: CompanyRecord) -> Enrichment | None:
        key = self._cache_key(company)
        if not key:
            return None

        if (cached := self.cache.get(key)) is not None:
            METRICS.incr("enrich.cache_hit")
            return self._to_enrichment(cached, cache_hit=True)

        # Single-flight: 200 records from the same parent company must not
        # produce 200 identical billable lookups.
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(company, key))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        else:
            METRICS.incr("enrich.coalesced")

        try:
            payload = await asyncio.shield(task)
        except FetchError as exc:
            METRICS.incr("enrich.failed")
            log.warning("enrich.failed", company=company.name, error=str(exc))
            return None
        if not payload:
            return None
        return self._to_enrichment(payload, cache_hit=False)

    async def _fetch(self, company: CompanyRecord, key: str) -> dict[str, Any]:
        params = {"domain": key} if not key.startswith("name:") else {"name": company.name}
        headers = {
            "Authorization": f"Bearer {self.settings.enrichment_api_key}",
            "Idempotency-Key": self.idempotency_key(company),
            "Accept": "application/json",
        }
        url = f"{self.settings.enrichment_base_url.rstrip('/')}/v1/companies/lookup"

        async with self._semaphore:
            with timed("enrich.lookup"):
                response = await self.client.get(url, headers=headers, params=params)

        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            log.warning("enrich.bad_json", url=url)
            return {}

        if payload.get("status") == "not_found":
            self.cache.set(key, {})  # negative caching: don't re-pay for a miss
            return {}

        self.cache.set(key, payload)
        METRICS.incr("enrich.lookup_ok")
        return payload

    async def enrich_many(self, companies: list[CompanyRecord]) -> list[Enrichment | None]:
        """Fan out with bounded concurrency; failures become None, never raise."""
        results = await asyncio.gather(*(self.enrich(c) for c in companies), return_exceptions=True)
        out: list[Enrichment | None] = []
        for company, result in zip(companies, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("enrich.exception", company=company.name, error=str(result))
                out.append(None)
            else:
                out.append(result)
        return out

    @staticmethod
    def _to_enrichment(payload: dict[str, Any], *, cache_hit: bool) -> Enrichment | None:
        if not payload:
            return None
        return Enrichment(
            provider=payload.get("provider", "demo-enrichment"),
            provider_company_id=payload.get("id"),
            industry=payload.get("industry"),
            naics=payload.get("naics"),
            revenue_usd=payload.get("revenue_usd"),
            employee_count=payload.get("employee_count"),
            linkedin_url=payload.get("linkedin_url"),
            technologies=payload.get("technologies") or [],
            confidence=float(payload.get("confidence") or 0.0),
            cache_hit=cache_hit,
        )
