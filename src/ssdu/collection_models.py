"""Portable collection specifications and integrity-protected lockfiles."""

import hashlib
import re
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from ssdu.contracts import QuerySpec, SnapshotId, StrictModel
from ssdu.providers import get_provider, provider_ids
from ssdu.store import canonical

Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
QUERY_ENGINE = "ssdu-query-v1"


class RequestSpec(StrictModel):
    provider: str = Field(json_schema_extra={"enum": list(provider_ids())})
    dataset_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    params: dict[str, Any] = Field(default_factory=dict)
    page_size: Annotated[int, Field(ge=1, le=10_000)] = 1000
    max_rows: Annotated[int, Field(ge=1, le=100_000)] = 10_000

    @field_validator("provider")
    @classmethod
    def registered_provider(cls, value):
        get_provider(value)
        return value


class CollectionEntry(StrictModel):
    request: RequestSpec | None = None
    snapshot_id: SnapshotId | None = None
    query: QuerySpec = Field(default_factory=QuerySpec)

    @model_validator(mode="after")
    def one_input(self):
        if (self.request is None) == (self.snapshot_id is None):
            raise ValueError("Each entry needs exactly one request or full snapshot ID")
        return self


class CollectionSpec(StrictModel):
    schema_version: Literal[1] = 1
    name: Name
    description: Annotated[str, StringConstraints(max_length=2000)] = ""
    entries: Annotated[dict[Name, CollectionEntry], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def reserve_identifiers(self):
        if re.fullmatch(r"[a-f0-9]{64}", self.name):
            raise ValueError("Full SHA-256 identifiers are reserved for lock references")
        return self


class LockedEntry(StrictModel):
    snapshot_id: SnapshotId
    dataset_id: str
    request: dict[str, Any]
    metadata_version: SnapshotId
    metadata: dict[str, Any]
    query: QuerySpec
    result_sha256: SnapshotId

    @model_validator(mode="after")
    def verify_metadata(self):
        if hashlib.sha256(canonical(self.metadata)).hexdigest() != self.metadata_version:
            raise ValueError("Locked metadata hash mismatch")
        if self.metadata.get("dataset_id") != self.dataset_id:
            raise ValueError("Locked metadata belongs to a different dataset")
        return self


class CollectionLock(StrictModel):
    schema_version: Literal[1] = 1
    lock_id: SnapshotId
    created_at: str
    spec: CollectionSpec
    entries: Annotated[dict[Name, LockedEntry], Field(min_length=1, max_length=50)]
    query_engine: Literal["ssdu-query-v1"] = QUERY_ENGINE
    software: dict[str, str]

    @model_validator(mode="after")
    def verify_identity(self):
        if set(self.entries) != set(self.spec.entries):
            raise ValueError("Lock entries differ from the collection specification")
        for name, entry in self.entries.items():
            intended = self.spec.entries[name]
            if entry.query != intended.query:
                raise ValueError("Locked query differs from the collection specification")
            if intended.snapshot_id and intended.snapshot_id != entry.snapshot_id:
                raise ValueError("Locked snapshot differs from the collection specification")
            if intended.request and intended.request.model_dump() != entry.request:
                raise ValueError("Locked request differs from the collection specification")
        identity = self.model_dump(exclude={"lock_id"})
        if hashlib.sha256(canonical(identity)).hexdigest() != self.lock_id:
            raise ValueError("Collection lock hash mismatch")
        return self


def result_identity(result):
    """Pin bounded query values and pagination independently of mutable registry state."""
    return {
        k: result[k] for k in ("rows", "total_rows", "truncated", "next_offset", "effective_query")
    }


def result_hash(result):
    return hashlib.sha256(canonical(result_identity(result))).hexdigest()
