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


class DimensionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: SearchText = ""
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
