"""Ingest endpoints: a retried POST must not turn into a 500 or a second crawl."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from directory_pipeline.api.deps import Resources, get_resources, require_temporal
from directory_pipeline.api.routes_ingest import router
from directory_pipeline.config import Settings


class FakeHandle:
    def __init__(self, wid: str) -> None:
        self.id = wid
        self.result_run_id = "run-1"


class FakeTemporal:
    """Refuses a second start of a running id, as Temporal does."""

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.started: list[str] = []

    async def start_workflow(self, run: Any, arg: Any, *, id: str, task_queue: str) -> FakeHandle:
        if id in self.running:
            raise WorkflowAlreadyStartedError(id, "CrawlDirectoryWorkflow", run_id="run-1")
        self.running.add(id)
        self.started.append(id)
        return FakeHandle(id)


def app_with(temporal: FakeTemporal) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    resources = Resources(settings=Settings(), search=None, index=None)  # type: ignore[arg-type]
    app.dependency_overrides[get_resources] = lambda: resources
    app.dependency_overrides[require_temporal] = lambda: temporal
    return TestClient(app)


def test_reposting_a_running_crawl_returns_it_instead_of_failing():
    temporal = FakeTemporal()
    client = app_with(temporal)

    first = client.post("/ingest/crawl", json={"categories": ["software"]})
    again = client.post("/ingest/crawl", json={"categories": ["software"]})

    assert first.status_code == 202 and first.json()["status"] == "started"
    assert again.status_code == 202, again.text
    assert again.json() == {
        "workflow_id": first.json()["workflow_id"],
        "run_id": "run-1",
        "status": "already_running",
    }
    assert len(temporal.started) == 1


def test_reposting_a_running_reindex_returns_it_instead_of_failing():
    temporal = FakeTemporal()
    client = app_with(temporal)

    client.post("/ingest/reindex", json={"alias": "companies"})
    again = client.post("/ingest/reindex", json={"alias": "companies"})

    assert again.status_code == 202, again.text
    assert again.json()["status"] == "already_running"
    assert len(temporal.started) == 1
