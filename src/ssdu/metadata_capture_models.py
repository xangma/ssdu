"""Bounded, explicit inputs for complete provider metadata captures."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from ssdu.contracts import SnapshotId, StrictModel
from ssdu.metadata_sync_models import MetadataSyncSpec, ProviderSync
from ssdu.retrieval_models import JobBudget
from ssdu.store import canonical


class CaptureProvider(ProviderSync):
    detail: Literal["dataset", "codes"] = "dataset"
    max_pages: Annotated[StrictInt, Field(ge=1, le=1000)] = 1000
    max_datasets: Annotated[StrictInt, Field(ge=1, le=100_000)] = 100_000

    @model_validator(mode="after")
    def inventory_first(self):
        if self.dataset_ids:
            raise ValueError(
                "Full captures discover catalogue membership; use metadata sync for explicit IDs"
            )
        return self


class CaptureSpec(MetadataSyncSpec):
    providers: Annotated[dict[str, CaptureProvider], Field(min_length=1, max_length=5)]


def capture_budget():
    return JobBudget(max_requests=10_000, max_total_bytes=1_000_000_000)


class CaptureInput(StrictModel):
    spec: CaptureSpec
    offline: StrictBool = False
    refresh: StrictBool = False
    budget: JobBudget = Field(default_factory=capture_budget)
    batch_size: Annotated[StrictInt, Field(ge=1, le=100)] = 10

    @model_validator(mode="after")
    def policy(self):
        if self.offline and self.refresh:
            raise ValueError("Offline metadata capture cannot refresh providers")
        if len(canonical(self.model_dump())) > 60_000:
            raise ValueError("Metadata capture input exceeds 60 KB")
        return self


class CapturePlan(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: SnapshotId
    input: CaptureInput
    implementation: SnapshotId
    ready: StrictBool = True
    provider_access_required: StrictBool
    estimated_requests: int | None = None
    estimated_bytes: int | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identity(self):
        if (
            hashlib.sha256(canonical(self.model_dump(exclude={"plan_id"}))).hexdigest()
            != self.plan_id
        ):
            raise ValueError("Metadata capture plan hash mismatch")
        return self
