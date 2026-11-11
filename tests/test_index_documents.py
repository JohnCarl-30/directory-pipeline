"""Writing documents: what a re-index must carry over from the previous copy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from opensearchpy import AsyncOpenSearch

import directory_pipeline.search.index as index_module
from directory_pipeline.config import Settings
from directory_pipeline.domain.models import CompanyRecord, EnrichedCompany
from directory_pipeline.search.index import SearchIndex


def company(source_id: str, first_seen: datetime) -> EnrichedCompany:
    return EnrichedCompany(
        company=CompanyRecord(
            record_id=CompanyRecord.make_record_id("demo", source_id),
            source="demo",
            source_id=source_id,
            source_url=f"http://d.test/company/{source_id}",
            name=source_id.title(),
            first_seen_at=first_seen,
            last_seen_at=first_seen,
        )
    )


class FakeClient:
    def __init__(self, existing: dict[str, dict[str, Any]]) -> None:
        self.existing = existing

    async def mget(self, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {
            "docs": [
                {"_id": i, "found": i in self.existing, "_source": self.existing.get(i, {})}
                for i in body["ids"]
            ]
        }


async def test_reindexing_a_changed_page_keeps_its_original_first_seen_at(monkeypatch):
    january = datetime(2026, 1, 5, tzinfo=UTC)
    march = datetime(2026, 3, 9, tzinfo=UTC)
    old, new = company("acme", march), company("brandnew", march)
    client = FakeClient({old.company.record_id: {"first_seen_at": january.isoformat()}})
    written: list[dict[str, Any]] = []

    async def fake_bulk(_client: Any, actions: list[dict[str, Any]], **kw: Any):
        written.extend(actions)
        return len(actions), []

    monkeypatch.setattr(index_module, "async_bulk", fake_bulk)
    idx = SearchIndex(Settings(), client=cast("AsyncOpenSearch", client))

    assert await idx.index_documents([old, new]) == 2

    by_id = {a["_id"]: a["_source"] for a in written}
    assert by_id[old.company.record_id]["first_seen_at"] == january.isoformat()
    assert by_id[old.company.record_id]["last_seen_at"] == march.isoformat()
    assert by_id[new.company.record_id]["first_seen_at"] == march.isoformat()
