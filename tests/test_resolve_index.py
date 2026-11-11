"""Index-wide resolution: the end-of-crawl pass that replaced per-batch dedupe.

Per-batch resolution compared only records that shared a batch of 25. With
incremental crawl a batch holds only the pages that changed, so a duplicate
whose page changed was resolved against nothing, written back as canonical,
and reappeared in search beside the record it duplicates.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from directory_pipeline.config import Settings
from directory_pipeline.domain.models import Address, CompanyRecord, Contact
from directory_pipeline.extraction.normalize import normalize_company_name
from directory_pipeline.orchestration.activities import PipelineActivities
from directory_pipeline.resolution.entity import (
    Candidate,
    IndexedRecord,
    cluster_updates,
    resolve,
    split_borderline,
)


def record(source_id: str, name: str, *, website: str, city: str = "Austin") -> CompanyRecord:
    return CompanyRecord(
        record_id=CompanyRecord.make_record_id("demo", source_id),
        source="demo",
        source_id=source_id,
        source_url=f"http://d.test/company/{source_id}",
        name=name,
        name_normalized=normalize_company_name(name),
        address=Address(city=city, region="TX", postal_code="78701"),
        contact=Contact(website=website, phone_e164="+15125550100"),
    )


def row(rec: CompanyRecord, *, cluster: str | None = None, dup: str | None = None, touched=False):
    return IndexedRecord(record=rec, cluster_id=cluster, duplicate_of=dup, touched=touched)


class FakeIndex:
    def __init__(self, rows: list[IndexedRecord]) -> None:
        self.rows = rows
        self.since: Any = "unset"
        self.applied: dict[str, tuple[str, str | None]] = {}

    async def load_for_resolution(self, *, alias=None, since=None, on_page=None):
        self.since = since
        return self.rows

    async def apply_clusters(self, updates, *, alias=None):
        self.applied = dict(updates)
        return len(updates)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def activities() -> PipelineActivities:
    return PipelineActivities(
        Settings(directory_base_url="http://d.test", enrichment_base_url="http://e.test")
    )


async def test_a_changed_duplicate_stays_a_duplicate(activities):
    """The reported bug, end to end: B was a duplicate of A, B's page changed,
    the batch re-indexed B alone as canonical. The closing pass puts it back."""
    a = record("acme", "Acme Systems", website="https://acme.example")
    b = record("acme-systems-inc", "Acme Systems, Inc.", website="https://www.acme.example")
    cluster = min(a.record_id, b.record_id)
    canonical = max(a.record_id, b.record_id)  # equal completeness: ties go to the larger id
    other = b if canonical == a.record_id else a
    index = FakeIndex(
        [
            row(a if canonical == a.record_id else b, cluster=cluster),
            # Just re-written by a batch: verdict reset, and touched.
            row(other, touched=True),
        ]
    )
    activities.index = index

    duplicates = await ActivityEnvironment().run(
        activities.resolve_index, None, "2026-11-11T00:00:00+00:00"
    )

    assert duplicates == 1
    assert index.applied == {other.record_id: (cluster, canonical)}
    assert index.since is not None and index.since.year == 2026


async def test_duplicates_in_different_batches_are_merged(activities):
    """Both written this crawl, by different batches, never compared before."""
    a = record("acme", "Acme Systems", website="https://acme.example")
    b = record("acme-systems-inc", "Acme Systems, Inc.", website="https://acme.example")
    index = FakeIndex([row(a, touched=True), row(b, touched=True)])
    activities.index = index

    assert await ActivityEnvironment().run(activities.resolve_index, None, None) == 1
    assert {cid for cid, _ in index.applied.values()} == {min(a.record_id, b.record_id)}


async def test_an_unchanged_corpus_writes_nothing(activities):
    a = record("acme", "Acme Systems", website="https://acme.example")
    b = record("acme-systems-inc", "Acme Systems, Inc.", website="https://acme.example")
    cluster_of, canonical_of = resolve([a, b])
    rows = [
        row(
            r,
            cluster=cluster_of[r.record_id],
            dup=None if canonical_of[r.record_id] == r.record_id else canonical_of[r.record_id],
        )
        for r in (a, b)
    ]
    index = FakeIndex(rows)
    activities.index = index

    await ActivityEnvironment().run(activities.resolve_index, None, None)
    assert index.applied == {}


def test_cluster_ids_do_not_depend_on_merge_order():
    a = record("acme", "Acme Systems", website="https://acme.example")
    b = record("acme-systems-inc", "Acme Systems, Inc.", website="https://acme.example")
    assert resolve([a, b])[0] == resolve([b, a])[0]


def _borderline(left: CompanyRecord, right: CompanyRecord) -> Candidate:
    return Candidate(left=left, right=right, score=0.5)


def test_untouched_borderline_pairs_keep_their_earlier_verdict():
    """The adjudicator is paid for what changed, not for the size of the corpus."""
    a = record("a", "Alpha", website="https://alpha.example")
    b = record("b", "Alpha Co", website="https://alpha.example", city="Dallas")
    c = record("c", "Gamma", website="https://gamma.example")
    d = record("d", "Gamma Co", website="https://gamma.example", city="Dallas")
    e = record("e", "Delta", website="https://delta.example")
    f = record("f", "Delta Co", website="https://delta.example", city="Dallas")
    rows = [
        row(a, cluster="k1"),
        row(b, cluster="k1"),  # accepted last time, unchanged
        row(c, cluster="k2"),
        row(d, cluster="k3"),  # kept apart last time, unchanged
        row(e, cluster="k4"),
        row(f, cluster="k4", touched=True),  # changed: ask again
    ]
    pairs = [_borderline(a, b), _borderline(c, d), _borderline(e, f)]

    reused, fresh = split_borderline(pairs, rows)

    assert [(p.left.source_id, p.right.source_id) for p in reused] == [("a", "b")]
    assert [(p.left.source_id, p.right.source_id) for p in fresh] == [("e", "f")]


def test_cluster_updates_report_only_changed_verdicts():
    a = record("a", "Alpha", website="https://alpha.example")
    b = record("b", "Beta", website="https://beta.example")
    rows = [row(a, cluster=a.record_id), row(b, cluster=None)]
    updates = cluster_updates(
        rows,
        {a.record_id: a.record_id, b.record_id: b.record_id},
        {a.record_id: a.record_id, b.record_id: b.record_id},
    )
    assert updates == {b.record_id: (b.record_id, None)}
