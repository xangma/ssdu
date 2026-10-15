"""Strict, local-only inputs for provider metadata synchronisation."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from ssdu.contracts import SnapshotId, StrictModel
from ssdu.providers import get_provider
from ssdu.retrieval_models import JobBudget
from ssdu.store import canonical

NativeId = Annotated[
    str, StringConstraints(min_length=1, max_length=2048, pattern=r"^[^\x00-\x1f\x7f]+$")
]


class ProviderSync(StrictModel):
    query: Annotated[str, StringConstraints(max_length=512)] = ""
    detail: Literal["catalogue", "dataset", "codes"] = "catalogue"
    page_size: Annotated[StrictInt, Field(ge=1, le=100)] = 100
    max_pages: Annotated[StrictInt, Field(ge=1, le=1000)] = 20
    max_datasets: Annotated[StrictInt, Field(ge=1, le=100_000)] = 1000
    max_code_pages: Annotated[StrictInt, Field(ge=1, le=1000)] = 5
    dataset_ids: Annotated[list[NativeId], Field(max_length=1000)] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_ids(self):
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError("Explicit dataset identifiers must be distinct")
        if self.dataset_ids and self.query.strip():
            raise ValueError("Use explicit dataset identifiers or a catalogue query")
        if self.dataset_ids and self.detail == "catalogue":
            raise ValueError("Explicit dataset identifiers require dataset or codes detail")
        if len(self.dataset_ids) > self.max_datasets:
            raise ValueError("Explicit dataset identifiers exceed max_datasets")
        return self


class MetadataSyncSpec(StrictModel):
    providers: Annotated[dict[str, ProviderSync], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def registered_workflows(self):
        for provider in self.providers:
            if not get_provider(provider).workflow:
                raise ValueError(f"Provider has no common metadata workflow: {provider}")
        return self


class SyncInput(StrictModel):
    spec: MetadataSyncSpec
    offline: StrictBool = False
    refresh: StrictBool = False
    budget: JobBudget = Field(default_factory=JobBudget)

    @model_validator(mode="after")
    def policy(self):
        if self.offline and self.refresh:
            raise ValueError("Offline metadata sync cannot refresh providers")
        if len(canonical(self.model_dump())) > 60_000:
            raise ValueError("Metadata sync input exceeds 60 KB")
        return self


class SyncPlan(StrictModel):
    schema_version: Literal[1] = 1
    plan_id: SnapshotId
    input: SyncInput
    implementation: SnapshotId
    ready: StrictBool = True
    provider_access_required: StrictBool
    estimated_requests: int | None = None
    estimated_bytes: int | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identity(self):
        import hashlib

        if (
            hashlib.sha256(canonical(self.model_dump(exclude={"plan_id"}))).hexdigest()
            != self.plan_id
        ):
            raise ValueError("Metadata sync plan hash mismatch")
        return self
