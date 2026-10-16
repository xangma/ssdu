"""Bounded, transport-independent contracts for local snapshot queries."""

import json
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]
SnapshotId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Column = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ProviderDimension = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
MetadataDatasetId = Annotated[
    str, StringConstraints(min_length=1, max_length=2100, pattern=r"^[^\x00-\x1f\x7f]+$")
]
MaxMetadataAge = Annotated[StrictInt, Field(ge=0, le=315_360_000)]
SearchText = Annotated[str, StringConstraints(max_length=512)]
Limit = Annotated[StrictInt, Field(ge=1, le=100)]
Offset = Annotated[StrictInt, Field(ge=0, le=100_000)]
Scalar = (
    Annotated[str, StringConstraints(max_length=512)] | StrictInt | StrictFloat | StrictBool | None
)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class Filter(StrictModel):
    column: Column
    op: Literal["eq", "in", "gte", "lte"] = "eq"
    value: Scalar | Annotated[list[Scalar], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def validate_operand(self):
        if (self.op == "in") != isinstance(self.value, list):
            raise ValueError("IN requires a list; other operators require a scalar")
        return self


class Sort(StrictModel):
    column: Column
    descending: StrictBool = False


class QuerySpec(StrictModel):
    columns: Annotated[list[Column], Field(min_length=1, max_length=100)] | None = None
    filters: Annotated[list[Filter], Field(max_length=20)] = Field(default_factory=list)
    sort: Annotated[list[Sort], Field(max_length=10)] = Field(default_factory=list)
    limit: Limit = 20
    offset: Offset = 0
    count_only: StrictBool = False


class DatasetQuerySpec(QuerySpec):
    """Query one registered contract table; omission selects the dataset profile."""

    table: Literal["observations", "records", "source"] | None = None


class SearchParams(BaseModel):
    # HTTP query strings require normal integer parsing. The MCP equivalent uses StrictInt.
    model_config = ConfigDict(extra="forbid")
    query: SearchText = ""
    provider: Identifier | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0


class MetadataSearchParams(SearchParams):
    include_retired: bool = False


class MetadataDescribeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: SnapshotId | None = None


class MetadataStatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Identifier | None = None
    max_age_seconds: Annotated[int, Field(ge=0, le=315_360_000)] = 86400


class DimensionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: SearchText = ""
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0


class ConnectorSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: SearchText = ""
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0


class QueryResult(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    rows: Annotated[list[dict[str, Any]], Field(max_length=100)]
    total_rows: Annotated[int, Field(ge=0)]
    truncated: bool
    next_offset: Annotated[int, Field(ge=0)] | None
    provenance: dict[str, Any]


class DataService(Protocol):
    def search_datasets(
        self, query: str = "", provider: str | None = None, limit: int = 20, offset: int = 0
    ) -> dict: ...

    def describe_dataset(self, dataset_id: str) -> dict: ...

    def list_dimension_values(
        self, dataset_id: str, dimension: str, search: str = "", limit: int = 20, offset: int = 0
    ) -> dict: ...

    def preview_data(self, snapshot_id: str, query: QuerySpec) -> dict: ...

    def query_data(self, snapshot_id: str, query: QuerySpec) -> dict: ...

    def describe_snapshot(self, snapshot_id: str) -> dict: ...

    def query_dataset(self, snapshot_id: str, query: DatasetQuerySpec) -> dict: ...

    def list_snapshot_dimension_values(
        self, snapshot_id: str, dimension: str, search: str = "", limit: int = 20, offset: int = 0
    ) -> dict: ...

    def get_provenance(self, snapshot_id: str) -> dict: ...


class ConnectorMetadataService(Protocol):
    """Optional cached-only extension for provider catalogue and selection metadata."""

    def list_provider_datasets(
        self, provider: str, query: str = "", limit: int = 20, offset: int = 0
    ) -> dict: ...

    def describe_provider_dataset(self, provider: str, dataset_id: str) -> dict: ...

    def list_provider_dimension_values(
        self,
        provider: str,
        dataset_id: str,
        dimension: str,
        search: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict: ...


def supports_connector_metadata(service) -> bool:
    """Keep older injected services usable without advertising unavailable methods."""
    return all(
        callable(getattr(service, name, None))
        for name in (
            "list_provider_datasets",
            "describe_provider_dataset",
            "list_provider_dimension_values",
        )
    )


def supports_metadata_sync(service, *, controls=False) -> bool:
    """Advertise metadata sync only when an injected service supplies every method."""
    names = (
        (
            "plan_metadata_sync",
            "get_metadata_plan",
            "submit_metadata_plan",
            "get_metadata_job",
            "list_metadata_jobs",
            "cancel_metadata_job",
            "retry_metadata_job",
        )
        if controls
        else (
            "search_metadata",
            "describe_metadata",
            "metadata_status",
            "list_metadata_snapshots",
            "describe_metadata_snapshot",
        )
    )
    return all(callable(getattr(service, name, None)) for name in names)


def supports_metadata_capture(service, *, controls=False) -> bool:
    """Advertise catalogue capture only when the injected service provides its interface."""
    names = (
        (
            "plan_metadata_capture",
            "get_metadata_capture_plan",
            "submit_metadata_capture_plan",
            "get_metadata_capture_job",
            "list_metadata_capture_jobs",
            "cancel_metadata_capture_job",
            "retry_metadata_capture_job",
        )
        if controls
        else ("metadata_capture_report",)
    )
    return all(callable(getattr(service, name, None)) for name in names)


def resolve_service(store="data", service: DataService | None = None) -> DataService:
    if service is not None:
        return service
    from .service import Service

    return Service(store)


def bounded_result(value: dict) -> dict:
    """Prevent an unexpectedly large provider document becoming an unbounded tool response."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 256_000:
        raise ValueError("Response exceeds the API output limit")
    return value
