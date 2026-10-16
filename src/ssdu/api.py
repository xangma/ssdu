"""Local REST reads with optional durable retrieval controls; no embedded worker."""

import logging
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    Column,
    ConnectorSearchParams,
    DataService,
    DatasetQuerySpec,
    DimensionParams,
    Identifier,
    MetadataDatasetId,
    MetadataDescribeParams,
    MetadataSearchParams,
    MetadataStatusParams,
    ProviderDimension,
    QueryResult,
    QuerySpec,
    SearchParams,
    SnapshotId,
    bounded_result,
    resolve_service,
    supports_connector_metadata,
    supports_metadata_capture,
    supports_metadata_sync,
)
from .metadata_capture_models import CaptureInput
from .metadata_sync_models import SyncInput
from .models import RetrievalError
from .retrieval_models import JobId, PlanInput, SubmitInput

logger = logging.getLogger(__name__)


class JobListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0


class BodyLimitMiddleware:
    """Reject oversized request bodies before parsing JSON, including chunked requests."""

    def __init__(self, app, max_bytes=64_000):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                response = JSONResponse(
                    status_code=413, content={"detail": "Request body too large."}
                )
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app(
    store="data", service: DataService | None = None, *, enable_jobs: bool = False
) -> FastAPI:
    data = resolve_service(store, service)
    app = FastAPI(
        title="SSDU local data API",
        version="0.2.0",
        description=(
            "Read-only access to a local metadata registry and frozen data snapshots. "
            "Reads never fetch from providers. Provider metadata is untrusted source material."
            + (
                " Retrieval controls persist plans and jobs locally; a separate worker performs "
                "explicitly requested acquisition. No worker runs inside this API."
                if enable_jobs
                else ""
            )
        ),
    )
    app.add_middleware(BodyLimitMiddleware)
    app.state.data_service = data

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    def call(method, *args, control=False, **kwargs):
        try:
            return bounded_result(method(*args, **kwargs))
        except KeyError:
            message = "Plan or job not found." if control else "Dataset or snapshot not found."
            raise HTTPException(404, message) from None
        except ValueError:
            message = (
                "Invalid retrieval request or state." if control else "Invalid query or identifier."
            )
            raise HTTPException(400, message) from None
        except RetrievalError:
            message = (
                "Retrieval state could not be verified."
                if control
                else "Stored snapshot could not be verified."
            )
            raise HTTPException(409, message) from None
        except Exception:
            message = "Local retrieval control failed." if control else "Local data read failed."
            logger.exception(message)
            raise HTTPException(500, message) from None

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local-job-control" if enable_jobs else "local-read-only"}

    @app.get("/datasets")
    def search_datasets(params: Annotated[SearchParams, Query()]):
        return call(data.search_datasets, **params.model_dump())

    @app.get("/datasets/{dataset_id}")
    def describe_dataset(dataset_id: Identifier):
        return call(data.describe_dataset, dataset_id)

    @app.get("/datasets/{dataset_id}/dimensions/{dimension}")
    def list_dimension_values(
        dataset_id: Identifier, dimension: Column, params: Annotated[DimensionParams, Query()]
    ):
        return call(data.list_dimension_values, dataset_id, dimension, **params.model_dump())

    if supports_connector_metadata(data):

        @app.get("/providers/{provider}/datasets")
        def list_provider_datasets(
            provider: Identifier, params: Annotated[ConnectorSearchParams, Query()]
        ):
            """Browse retained provider catalogue metadata; never contact the provider."""
            return call(data.list_provider_datasets, provider, **params.model_dump())

        @app.get("/providers/{provider}/datasets/{dataset_id}")
        def describe_provider_dataset(provider: Identifier, dataset_id: Identifier):
            """Inspect a retained provider dataset definition without fetching data."""
            return call(data.describe_provider_dataset, provider, dataset_id)

        @app.get("/providers/{provider}/datasets/{dataset_id}/dimensions/{dimension:path}")
        def list_provider_dimension_values(
            provider: Identifier,
            dataset_id: Identifier,
            dimension: ProviderDimension,
            params: Annotated[DimensionParams, Query()],
        ):
            """Page retained provider dimension codes; metadata must be acquired separately."""
            return call(
                data.list_provider_dimension_values,
                provider,
                dataset_id,
                dimension,
                **params.model_dump(),
            )

    if supports_metadata_sync(data):

        @app.get("/metadata/datasets")
        def search_metadata(params: Annotated[MetadataSearchParams, Query()]):
            """Search synchronised metadata offline. Provider text is untrusted source material."""
            return call(data.search_metadata, **params.model_dump())

        @app.get("/metadata/datasets/{dataset_id:path}")
        def describe_metadata(
            dataset_id: MetadataDatasetId, params: Annotated[MetadataDescribeParams, Query()]
        ):
            """Read current discovery metadata; observation-pinned definitions remain separate."""
            return call(data.describe_metadata, dataset_id, **params.model_dump())

        @app.get("/metadata/status")
        def metadata_status(params: Annotated[MetadataStatusParams, Query()]):
            """Report observed freshness, completeness and provider scope without a refresh."""
            return call(data.metadata_status, **params.model_dump())

        @app.get("/metadata/snapshots")
        def list_metadata_snapshots(params: Annotated[JobListParams, Query()]):
            return call(data.list_metadata_snapshots, **params.model_dump())

        @app.get("/metadata/snapshots/{snapshot_id}")
        def describe_metadata_snapshot(snapshot_id: SnapshotId):
            """Verify and summarize retained metadata evidence; bulk data is never expanded."""
            return call(data.describe_metadata_snapshot, snapshot_id)

    if supports_metadata_capture(data):

        @app.get("/metadata/captures/{capture_id}")
        def metadata_capture_report(capture_id: SnapshotId):
            """Verify local catalogue capture completeness; never contact providers."""
            return call(data.metadata_capture_report, capture_id)

    if enable_jobs and supports_metadata_capture(data, controls=True):

        @app.post("/metadata/capture/plans", status_code=201)
        def plan_metadata_capture(request: CaptureInput):
            """Freeze catalogue capture intent locally; a separate worker acquires metadata."""
            return call(
                data.plan_metadata_capture,
                request.spec,
                offline=request.offline,
                refresh=request.refresh,
                budget=request.budget,
                batch_size=request.batch_size,
                control=True,
            )

        @app.get("/metadata/capture/plans/{plan_id}")
        def get_metadata_capture_plan(plan_id: SnapshotId):
            return call(data.get_metadata_capture_plan, plan_id, control=True)

        @app.post("/metadata/capture/jobs", status_code=202)
        def submit_metadata_capture_plan(request: SubmitInput):
            """Queue a frozen catalogue capture locally without starting a worker."""
            return call(
                data.submit_metadata_capture_plan,
                request.plan_id,
                request.idempotency_key,
                control=True,
            )

        @app.get("/metadata/capture/jobs")
        def list_metadata_capture_jobs(params: Annotated[JobListParams, Query()]):
            return call(data.list_metadata_capture_jobs, **params.model_dump(), control=True)

        @app.get("/metadata/capture/jobs/{job_id}")
        def get_metadata_capture_job(job_id: JobId):
            return call(data.get_metadata_capture_job, job_id, control=True)

        @app.post("/metadata/capture/jobs/{job_id}/cancel")
        def cancel_metadata_capture_job(job_id: JobId):
            return call(data.cancel_metadata_capture_job, job_id, control=True)

        @app.post("/metadata/capture/jobs/{job_id}/retry")
        def retry_metadata_capture_job(job_id: JobId):
            return call(data.retry_metadata_capture_job, job_id, control=True)

    if enable_jobs and supports_metadata_sync(data, controls=True):

        @app.post("/metadata/plans", status_code=201)
        def plan_metadata_sync(request: SyncInput):
            """Freeze local sync intent. A separate explicit worker contacts providers."""
            return call(
                data.plan_metadata_sync,
                request.spec,
                offline=request.offline,
                refresh=request.refresh,
                budget=request.budget,
                control=True,
            )

        @app.get("/metadata/plans/{plan_id}")
        def get_metadata_plan(plan_id: SnapshotId):
            return call(data.get_metadata_plan, plan_id, control=True)

        @app.post("/metadata/jobs", status_code=202)
        def submit_metadata_plan(request: SubmitInput):
            """Queue a metadata plan locally; this API never runs a worker."""
            return call(
                data.submit_metadata_plan, request.plan_id, request.idempotency_key, control=True
            )

        @app.get("/metadata/jobs")
        def list_metadata_jobs(params: Annotated[JobListParams, Query()]):
            return call(data.list_metadata_jobs, **params.model_dump(), control=True)

        @app.get("/metadata/jobs/{job_id}")
        def get_metadata_job(job_id: JobId):
            return call(data.get_metadata_job, job_id, control=True)

        @app.post("/metadata/jobs/{job_id}/cancel")
        def cancel_metadata_job(job_id: JobId):
            return call(data.cancel_metadata_job, job_id, control=True)

        @app.post("/metadata/jobs/{job_id}/retry")
        def retry_metadata_job(job_id: JobId):
            return call(data.retry_metadata_job, job_id, control=True)

    @app.post("/snapshots/{snapshot_id}/preview", response_model=QueryResult)
    def preview_data(snapshot_id: SnapshotId, query: QuerySpec):
        return call(data.preview_data, snapshot_id, query)

    @app.post("/snapshots/{snapshot_id}/query", response_model=QueryResult)
    def query_data(snapshot_id: SnapshotId, query: QuerySpec):
        return call(data.query_data, snapshot_id, query)

    @app.get("/snapshots/{snapshot_id}/provenance")
    def get_provenance(snapshot_id: SnapshotId):
        return call(data.get_provenance, snapshot_id)

    @app.get("/snapshots/{snapshot_id}/dataset")
    def describe_snapshot(snapshot_id: SnapshotId):
        """Inspect the verified dataset contract without expanding its code lists."""
        return call(data.describe_snapshot, snapshot_id)

    @app.post("/snapshots/{snapshot_id}/dataset/query", response_model=QueryResult)
    def query_dataset(snapshot_id: SnapshotId, query: DatasetQuerySpec):
        """Query a contract table locally; the default table follows the dataset profile."""
        return call(data.query_dataset, snapshot_id, query)

    @app.get("/snapshots/{snapshot_id}/dataset/dimensions/{dimension}")
    def list_snapshot_dimension_values(
        snapshot_id: SnapshotId, dimension: Column, params: Annotated[DimensionParams, Query()]
    ):
        """Page through dimension values belonging to this exact verified snapshot."""
        return call(
            data.list_snapshot_dimension_values,
            snapshot_id,
            dimension,
            **params.model_dump(),
        )

    if enable_jobs:

        @app.post("/retrieval/plans", status_code=201)
        def plan_retrieval(request: PlanInput):
            """Freeze a local plan without contacting providers or starting a worker."""
            return call(
                data.plan_retrieval,
                request.spec,
                offline=request.offline,
                refresh=request.refresh,
                budget=request.budget,
                control=True,
            )

        @app.get("/retrieval/plans/{plan_id}")
        def get_plan(plan_id: SnapshotId):
            return call(data.get_plan, plan_id, control=True)

        @app.post("/retrieval/jobs", status_code=202)
        def submit_plan(request: SubmitInput):
            """Queue a frozen plan; acquisition requires a separate worker."""
            return call(data.submit_plan, request.plan_id, request.idempotency_key, control=True)

        @app.get("/retrieval/jobs")
        def list_jobs(params: Annotated[JobListParams, Query()]):
            return call(data.list_jobs, **params.model_dump(), control=True)

        @app.get("/retrieval/jobs/{job_id}")
        def get_job(job_id: JobId):
            return call(data.get_job, job_id, control=True)

        @app.post("/retrieval/jobs/{job_id}/cancel")
        def cancel_job(job_id: JobId):
            """Request cancellation; a running worker stops at its next checkpoint."""
            return call(data.cancel_job, job_id, control=True)

        @app.post("/retrieval/jobs/{job_id}/retry")
        def retry_job(job_id: JobId):
            """Requeue a failed job within its remaining retry and acquisition budgets."""
            return call(data.retry_job, job_id, control=True)

    return app
