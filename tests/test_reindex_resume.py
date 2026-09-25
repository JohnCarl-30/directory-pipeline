"""A retried reindex must continue the copy, not start a second one.

physical_index_name() is unique by construction -- timestamp plus a random
suffix -- so an attempt that recomputes it creates a fresh index and submits a
fresh copy task while the previous copy is still running on the cluster. Two
full copies compete for IO, and the first fills an index nothing will ever swap
to. On a large index that is hours of wasted work and leaked storage.

Temporal hands a retried activity its predecessor's last heartbeat details, so
the task id and target index recorded there are enough to reattach.
"""

from __future__ import annotations

from typing import Any

import pytest

from directory_pipeline.config import Settings
from directory_pipeline.orchestration.activities import _resume_from_heartbeat
from directory_pipeline.search.index import SearchIndex


class FakeIndices:
    def __init__(self, owner: FakeClient) -> None:
        self._o = owner

    async def get_alias(self, **kw: Any) -> dict[str, Any]:
        return {"companies-v1-old": {"aliases": {"companies": {}}}}

    async def create(self, index: str, **kw: Any) -> dict[str, Any]:
        self._o.created.append(index)
        return {"acknowledged": True}

    async def refresh(self, **kw: Any) -> dict[str, Any]:
        return {}

    async def update_aliases(self, **kw: Any) -> dict[str, Any]:
        self._o.alias_actions.append(kw.get("body", {}))
        return {"acknowledged": True}

    async def delete(self, **kw: Any) -> dict[str, Any]:
        return {"acknowledged": True}


class FakeIngest:
    async def put_pipeline(self, **kw: Any) -> dict[str, Any]:
        return {"acknowledged": True}


class FakeTasks:
    def __init__(self, owner: FakeClient) -> None:
        self._o = owner

    async def get(self, task_id: str, **kw: Any) -> dict[str, Any]:
        self._o.polled.append(task_id)
        # Report one in-progress poll, then completion, so on_progress fires.
        if len(self._o.polled) < 2:
            return {
                "completed": False,
                "task": {"status": {"created": 5, "total": 10}},
            }
        return {"completed": True, "response": {"created": 10, "updated": 0}}


class FakeClient:
    def __init__(self) -> None:
        self.indices = FakeIndices(self)
        self.ingest = FakeIngest()
        self.tasks = FakeTasks(self)
        self.created: list[str] = []
        self.submitted: list[dict[str, Any]] = []
        self.polled: list[str] = []
        self.alias_actions: list[dict[str, Any]] = []

    async def reindex(self, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.submitted.append(body)
        return {"task": "node-1:9999"}


@pytest.fixture
def index() -> tuple[SearchIndex, FakeClient]:
    client = FakeClient()
    settings = Settings(
        directory_base_url="http://directory.test",
        enrichment_base_url="http://enrich.test",
        opensearch_alias="companies",
    )
    return SearchIndex(settings, client=client), client


async def test_a_fresh_reindex_creates_an_index_and_submits_a_copy(index):
    idx, client = index
    progress: list[dict[str, Any]] = []

    result = await idx.reindex(alias="companies", on_progress=progress.append, poll_interval_s=0.01)

    assert len(client.created) == 1, "a fresh run creates its target"
    assert len(client.submitted) == 1, "a fresh run submits one copy"
    assert result["swapped"] is True
    # The task id and target must reach the caller, or a retry has nothing to
    # reattach to.
    assert progress and progress[0]["task_id"] == "node-1:9999"
    assert progress[0]["target_index"] == client.created[0]


async def test_a_resumed_reindex_submits_no_second_copy(index):
    idx, client = index
    resume = {"task_id": "node-1:9999", "target_index": "companies-v1-inflight"}

    result = await idx.reindex(alias="companies", resume=resume, poll_interval_s=0.01)

    assert client.submitted == [], "resuming must not start another copy"
    assert client.created == [], "resuming must not create another index"
    assert client.polled == ["node-1:9999", "node-1:9999"], "it polled the existing task"
    assert result["target_index"] == "companies-v1-inflight"
    assert result["swapped"] is True


async def test_a_resumed_reindex_swaps_the_index_the_copy_was_filling(index):
    """Reattaching to a task filling index A while swapping B publishes nothing."""
    idx, client = index
    resume = {"task_id": "node-1:9999", "target_index": "companies-v1-inflight"}

    await idx.reindex(alias="companies", resume=resume, poll_interval_s=0.01)

    added = [
        action["add"]["index"]
        for body in client.alias_actions
        for action in body["actions"]
        if "add" in action
    ]
    assert added == ["companies-v1-inflight"]


def test_resume_is_ignored_outside_an_activity():
    """Direct calls and unit tests have no heartbeat to read."""
    assert _resume_from_heartbeat() is None


def test_resume_requires_both_the_task_and_the_target(monkeypatch):
    """A task id alone would reattach to a copy filling an index we then ignore."""
    import directory_pipeline.orchestration.activities as mod

    class FakeInfo:
        def __init__(self, details):
            self.heartbeat_details = details

    for details, expected in [
        (({"task_id": "n:1"},), None),  # no target
        (({"target_index": "companies-v1"},), None),  # no task
        ((), None),  # first attempt
        (("not-a-dict",), None),
        (
            ({"task_id": "n:1", "target_index": "companies-v1"},),
            {"task_id": "n:1", "target_index": "companies-v1"},
        ),
    ]:
        monkeypatch.setattr(mod.activity, "info", lambda d=details: FakeInfo(d))
        assert mod._resume_from_heartbeat() == expected, details
