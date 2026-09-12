"""Ingest and index-management endpoints.

Every one of these starts a Temporal workflow and returns immediately with a
handle. None of them do the work in-process: a crawl runs for minutes to hours,
and an HTTP request is the wrong place to hold that.

Workflow ids are caller-supplied or deterministic, which makes the endpoints
idempotent -- POSTing the same crawl twice attaches to the running one instead
of starting a second.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from temporalio.service import RPCError

from ..domain.models import CrawlRequest, ReindexRequest
from ..observability import METRICS
from .deps import Resources, get_resources, require_temporal

router = APIRouter(prefix="/ingest", tags=["ingest"])


class WorkflowHandleResponse(BaseModel):
    workflow_id: str
    run_id: str
    status: str = "started"


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    run_id: str | None = None
    status: str
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


@router.post("/crawl", response_model=WorkflowHandleResponse, status_code=202)
async def start_crawl(
    request: CrawlRequest,
    resources: Annotated[Resources, Depends(get_resources)],
    client: Annotated[Any, Depends(require_temporal)],
    workflow_id: str | None = None,
) -> WorkflowHandleResponse:
    from ..orchestration.workflows import CrawlDirectoryWorkflow

    payload = request.model_copy(
        update={"index_alias": request.index_alias or resources.settings.opensearch_alias}
    )
    # Deterministic id -> re-POSTing the same crawl is a no-op, not a duplicate.
    wid = workflow_id or f"crawl-{payload.source}-{'-'.join(sorted(payload.categories))}"

    handle = await client.start_workflow(
        CrawlDirectoryWorkflow.run,
        payload,
        id=wid,
        task_queue=resources.settings.temporal_task_queue,
    )
    METRICS.incr("api.crawls_started")
    return WorkflowHandleResponse(workflow_id=handle.id, run_id=handle.result_run_id or "")


@router.post("/reindex", response_model=WorkflowHandleResponse, status_code=202)
async def start_reindex(
    request: ReindexRequest,
    resources: Annotated[Resources, Depends(get_resources)],
    client: Annotated[Any, Depends(require_temporal)],
) -> WorkflowHandleResponse:
    """Kick off a zero-downtime reindex behind the alias."""
    from ..orchestration.workflows import ReindexWorkflow

    handle = await client.start_workflow(
        ReindexWorkflow.run,
        request,
        id=f"reindex-{request.alias}-{request.target_schema_version}",
        task_queue=resources.settings.temporal_task_queue,
    )
    METRICS.incr("api.reindexes_started")
    return WorkflowHandleResponse(workflow_id=handle.id, run_id=handle.result_run_id or "")


@router.get("/workflows/{workflow_id}", response_model=WorkflowStatusResponse)
async def workflow_status(
    workflow_id: str, client: Annotated[Any, Depends(require_temporal)]
) -> WorkflowStatusResponse:
    """Status plus live progress, read from the workflow's own query handlers."""
    handle = client.get_workflow_handle(workflow_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        raise HTTPException(404, f"No workflow {workflow_id}: {exc}") from exc

    status = description.status.name if description.status else "UNKNOWN"
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None

    if status == "RUNNING":
        try:
            progress = await handle.query("progress")
        except Exception:
            progress = None  # not every workflow type exposes this query
    elif status == "COMPLETED":
        payload = await handle.result()
        result = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload

    return WorkflowStatusResponse(
        workflow_id=workflow_id,
        run_id=description.run_id,
        status=status,
        progress=progress,
        result=result,
    )


@router.post("/workflows/{workflow_id}/stop", status_code=202)
async def stop_workflow(
    workflow_id: str, client: Annotated[Any, Depends(require_temporal)]
) -> dict[str, str]:
    """Graceful stop: in-flight batches finish, no new ones start.

    Distinct from cancel/terminate -- the workflow decides when to wind down,
    so partially-processed batches still get indexed.
    """
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.signal("stop")
    except RPCError as exc:
        raise HTTPException(404, f"No workflow {workflow_id}: {exc}") from exc
    return {"workflow_id": workflow_id, "status": "stop signal sent"}


class IndexStatsResponse(BaseModel):
    alias: str
    index: str | None
    documents: int = 0
    reachable: bool = True


@router.get("/index/stats", response_model=IndexStatsResponse)
async def index_stats(
    resources: Annotated[Resources, Depends(get_resources)], alias: str | None = None
) -> IndexStatsResponse:
    target = alias or resources.settings.opensearch_alias
    if not await resources.index.ping():
        return IndexStatsResponse(alias=target, index=None, reachable=False)
    stats = await resources.index.stats(target)
    return IndexStatsResponse(**stats, reachable=True)


class RollbackRequest(BaseModel):
    alias: str
    to_index: str = Field(description="Physical index name to point the alias back at.")


@router.post("/index/rollback", status_code=200)
async def rollback(
    request: RollbackRequest, resources: Annotated[Resources, Depends(get_resources)]
) -> dict[str, str]:
    """Repoint the alias at a previous index. The reason old indices are kept."""
    await resources.index.rollback(request.alias, request.to_index)
    return {"alias": request.alias, "now_pointing_at": request.to_index}
