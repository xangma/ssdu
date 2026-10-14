"""Bounded contracts for local retrieval plans and durable collection jobs."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from ssdu.collection_models import CollectionSpec
from ssdu.contracts import SnapshotId, StrictModel
from ssdu.store import canonical

JobId = Annotated[str, StringConstraints(pattern=r"^job_[a-f0-9]{32}$")]
IdempotencyKey = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]


class JobBudget(StrictModel):
    max_requests: Annotated[StrictInt, Field(ge=1, le=10_000)] = 128
    max_total_bytes: Annotated[StrictInt, Field(ge=1, le=1_000_000_000)] = 100_000_000
    max_attempts: Annotated[StrictInt, Field(ge=1, le=10)] = 3
    retry_delay_seconds: Annotated[float, Field(ge=0, le=3600)] = 1.0


class PlanInput(StrictModel):
    spec: CollectionSpec
    offline: StrictBool = False
    refresh: StrictBool = False
    budget: JobBudget = Field(default_factory=JobBudget)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.offline and self.refresh:
            raise ValueError("Offline retrieval cannot refresh providers")
        if len(canonical(self.model_dump())) > 60_000:
            raise ValueError("Retrieval input exceeds 60 KB")
        return self


class SubmitInput(StrictModel):
    plan_id: SnapshotId
    idempotency_key: IdempotencyKey


class PlanEntry(StrictModel):
    action: Literal["snapshot", "cached", "derive", "fetch", "blocked"]
    dataset_id: str
    snapshot_id: SnapshotId | None = None
    source_snapshot_id: SnapshotId | None = None
    legacy_snapshot_id: SnapshotId | None = None
    metadata_version: SnapshotId | None = None
    known_rows: int | None = None
    selector_validation: Literal["validated", "pending", "invalid"] = "pending"
    query_validation: Literal["validated", "pending", "invalid"] = "pending"
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RetrievalPlan(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: SnapshotId
    input: PlanInput
    implementation: SnapshotId
    entries: dict[str, PlanEntry]
    ready: StrictBool
    provider_access_required: StrictBool
    estimated_requests: int | None = None
    estimated_bytes: int | None = None

    @model_validator(mode="after")
    def check_identity(self):
        content = self.model_dump(exclude={"plan_id"})
        if hashlib.sha256(canonical(content)).hexdigest() != self.plan_id:
            raise ValueError("Retrieval plan hash mismatch")
        if set(self.entries) != set(self.input.spec.entries):
            raise ValueError("Plan entries differ from collection entries")
        return self
