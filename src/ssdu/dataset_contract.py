"""Versioned, source-bound Arrow datasets with validated observation semantics.

The contract describes the data rather than its immutable Store snapshot identity.
A Store binds ``snapshot_id`` only after hashing and verifying the surrounding files.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from ssdu.contracts import SnapshotId, StrictModel

CONTRACT_VERSION = "1.0.0"
RECORDS_CONTRACT_VERSION = "1.1.0"
SUPPORTED_CONTRACT_VERSIONS = (CONTRACT_VERSION, RECORDS_CONTRACT_VERSION)
MAX_DESCRIPTOR_BYTES = 8 * 1024 * 1024
Name = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
]
Text = Annotated[str, StringConstraints(max_length=100_000)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Count = Annotated[StrictInt, Field(ge=0)]
DType = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Role = Literal[
    "dimension",
    "measure",
    "value",
    "value_raw",
    "unit",
    "status",
    "notes",
    "attribute",
    "identifier",
]


class DatasetContractError(ValueError):
    """A dataset does not satisfy its declared version or table semantics."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _json_tree(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("Dataset metadata exceeds nesting limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Dataset metadata contains a non-finite number")
        return
    if type(value) is list:
        for item in value:
            _json_tree(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_tree(item, depth + 1)
        return
    raise ValueError("Dataset metadata must contain JSON values")


class RawInput(StrictModel):
    path: ShortText
    sha256: SnapshotId
    bytes: Count

    @model_validator(mode="after")
    def safe_path(self):
        parts = self.path.split("/")
        if (
            self.path.startswith("/")
            or "\\" in self.path
            or re.match(r"^[A-Za-z]:", self.path)
            or any(ord(character) < 32 for character in self.path)
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("Raw input path must be a safe relative path")
        return self


class Source(StrictModel):
    provider: ShortText
    dataset_id: ShortText
    url: Text
    licence: Text
    citation: Text
    release: dict[str, Any] = Field(default_factory=dict)
    effective_request: dict[str, Any] = Field(default_factory=dict)
    raw_inputs: Annotated[list[RawInput], Field(max_length=10_000)] = Field(default_factory=list)
    parent_snapshot_ids: Annotated[list[SnapshotId], Field(max_length=128)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def unique_bindings(self):
        paths = [item.path for item in self.raw_inputs]
        if len(paths) != len(set(paths)):
            raise ValueError("Raw input paths must be unique")
        if len(self.parent_snapshot_ids) != len(set(self.parent_snapshot_ids)):
            raise ValueError("Parent snapshot bindings must be unique")
        return self


class Transform(StrictModel):
    id: Name
    version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$", max_length=40)]


class Code(StrictModel):
    code: ShortText
    label: Text | None = None


class Dimension(StrictModel):
    id: Name
    column: Name
    label: Text
    kind: Literal["geography", "time", "category", "identifier"] = "category"
    codes: Annotated[list[Code], Field(max_length=100_000)] = Field(default_factory=list)
    nullable: StrictBool = False
    level: Text | None = None
    vintage: Text | None = None

    @model_validator(mode="after")
    def unique_codes(self):
        codes = [item.code for item in self.codes]
        if len(codes) != len(set(codes)):
            raise ValueError("Dimension codes must be unique")
        return self


class Variable(StrictModel):
    id: ShortText
    label: Text
    unit: Text | None = None
    description: Text | None = None
    kind: Literal[
        "measure",
        "rank",
        "decile",
        "score",
        "count",
        "rate",
        "percentage",
        "population",
        "denominator",
    ] = "measure"
    source_column: Text | None = None


class Column(StrictModel):
    name: ShortText
    dtype: DType
    role: Role
    nullable: StrictBool = True
    label: Text | None = None
    definition: Text | None = None
    unit: Text | None = None
    missing_values: Annotated[dict[Text, Text], Field(max_length=100)] = Field(default_factory=dict)

    @field_validator("dtype")
    @classmethod
    def supported_dtype(cls, value):
        arrow_type(value)
        return value

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        for field in ("label", "definition", "unit"):
            if value.get(field) is None:
                value.pop(field, None)
        if not value.get("missing_values"):
            value.pop("missing_values", None)
        return value


class Table(StrictModel):
    name: Literal["observations", "records", "source"]
    path: ShortText
    rows: Count
    columns: Annotated[list[Column], Field(min_length=1, max_length=10_000)]

    @model_validator(mode="after")
    def validate_columns(self):
        if self.path != f"tables/{self.name}.parquet":
            raise ValueError("Table path must match its canonical contract path")
        names = [item.name for item in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("Table column names must be unique")
        return self


class Diagnostic(StrictModel):
    code: Name
    message: Text
    column: Name | None = None
    count: Count | None = None


class Quality(StrictModel):
    source_rows: Count
    observation_rows: Count
    records_rows: Count | None = None
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    diagnostics: Annotated[list[Diagnostic], Field(max_length=10_000)] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if value.get("records_rows") is None:
            value.pop("records_rows", None)
        return value


class Coverage(StrictModel):
    geography: dict[str, Any] = Field(default_factory=lambda: {"status": "unknown"})
    temporal: dict[str, Any] = Field(default_factory=lambda: {"status": "unknown"})
    population: Text | None = None


class DatasetDescriptor(StrictModel):
    contract_version: Literal["1.0.0", "1.1.0"]
    profile: Literal["observations", "records"]
    dataset_id: ShortText
    title: Text
    source: Source
    transform: Transform
    dimensions: Annotated[list[Dimension], Field(max_length=100)] = Field(default_factory=list)
    variables: Annotated[list[Variable], Field(max_length=10_000)] = Field(default_factory=list)
    tables: Annotated[list[Table], Field(min_length=1, max_length=2)]
    quality: Quality
    coverage: Coverage = Field(default_factory=Coverage)
    primary_key: Annotated[list[ShortText], Field(max_length=100)] = Field(default_factory=list)
    row_grain: Text | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if self.profile == "observations":
            value.pop("primary_key", None)
            value.pop("row_grain", None)
        return value

    @model_validator(mode="after")
    def validate_structure(self):
        expected_version = (
            CONTRACT_VERSION if self.profile == "observations" else RECORDS_CONTRACT_VERSION
        )
        if self.contract_version != expected_version:
            raise ValueError("Dataset profile does not support this contract version")
        if self.dataset_id != f"{self.source.provider}:{self.source.dataset_id}":
            raise ValueError("Dataset identity differs from its source provider and identifier")
        for values, attribute, description in (
            (self.dimensions, "id", "Dimension identifiers"),
            (self.dimensions, "column", "Dimension columns"),
            (self.variables, "id", "Variable identifiers"),
            (self.tables, "name", "Table names"),
        ):
            names = [getattr(item, attribute) for item in values]
            if len(names) != len(set(names)):
                raise ValueError(f"{description} must be unique")
        tables = {item.name: item for item in self.tables}
        if self.profile == "records":
            self._validate_records(tables)
        else:
            self._validate_observations(tables)
        value = self.model_dump()
        _json_tree(value)
        if len(_canonical(value)) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("Dataset descriptor exceeds the 8 MiB limit")
        return self

    def _validate_records(self, tables):
        if "records" not in tables or "observations" in tables:
            raise ValueError("Records profile requires a records table and optional source table")
        if self.dimensions or self.variables:
            raise ValueError("Records fields do not imply observation dimensions or variables")
        if self.quality.records_rows != tables["records"].rows:
            raise ValueError("Records row count differs from quality metadata")
        if self.quality.observation_rows:
            raise ValueError("Records profile has no observation rows")
        if "source" in tables and tables["source"].rows != self.quality.source_rows:
            raise ValueError("Source row count differs from quality metadata")
        if len(self.primary_key) != len(set(self.primary_key)):
            raise ValueError("Primary key columns must be unique")
        columns = {column.name: column for column in tables["records"].columns}
        for name in self.primary_key:
            if name not in columns or columns[name].nullable:
                raise ValueError("Primary key requires declared nonnullable records columns")
        for table in tables.values():
            if any(column.role not in {"attribute", "identifier"} for column in table.columns):
                raise ValueError("Records columns require attribute or identifier roles")

    def _validate_observations(self, tables):
        if self.primary_key or self.row_grain is not None or self.quality.records_rows is not None:
            raise ValueError("Observation contract 1.0.0 does not support records metadata")
        if not self.variables:
            raise ValueError("Observation profile requires variable definitions")
        if "records" in tables:
            raise ValueError("Observation profile cannot contain a records table")
        for table in tables.values():
            for column in table.columns:
                if column.dtype not in _ARROW_TYPES or column.role == "identifier":
                    raise ValueError("Observation contract 1.0.0 has unsupported column metadata")
                if (
                    column.label is not None
                    or column.definition is not None
                    or column.unit is not None
                    or column.missing_values
                ):
                    raise ValueError(
                        "Observation contract 1.0.0 does not support record field metadata"
                    )
        if "observations" not in tables:
            raise ValueError("Observation profile requires an observations table")
        if tables["observations"].rows != self.quality.observation_rows:
            raise ValueError("Observation row count differs from quality metadata")
        if "source" in tables and tables["source"].rows != self.quality.source_rows:
            raise ValueError("Source row count differs from quality metadata")
        required = {
            "measure": ("string", "measure"),
            "value": ("float64", "value"),
            "value_raw": ("string", "value_raw"),
            "unit": ("string", "unit"),
            "status": ("string", "status"),
            "notes": ("string", "notes"),
        }
        columns = {item.name: item for item in tables["observations"].columns}
        for name, (dtype, role) in required.items():
            if name not in columns or (columns[name].dtype, columns[name].role) != (dtype, role):
                raise ValueError(f"Observation column {name} requires {dtype} and role {role}")
        if columns["measure"].nullable:
            raise ValueError("Measure identifier cannot be nullable")
        dimension_columns = {item.column for item in self.dimensions}
        declared_dimensions = {name for name, item in columns.items() if item.role == "dimension"}
        if declared_dimensions != dimension_columns or dimension_columns.intersection(required):
            raise ValueError("Observation dimension roles differ from dimension metadata")
        for dimension in self.dimensions:
            column = columns[dimension.column]
            if column.dtype != "string" or column.nullable != dimension.nullable:
                raise ValueError("Dimension column type or nullability differs from metadata")
        for name, column in columns.items():
            if (
                name not in required
                and name not in dimension_columns
                and column.role != "attribute"
            ):
                raise ValueError("Additional observation fields must have attribute role")


_ARROW_TYPES = {
    "string": pa.string(),
    "large_string": pa.large_string(),
    "int64": pa.int64(),
    "uint64": pa.uint64(),
    "float64": pa.float64(),
    "bool": pa.bool_(),
    "date32": pa.date32(),
    "timestamp[ns]": pa.timestamp("ns"),
    "null": pa.null(),
}


def arrow_dtype(value: pa.DataType) -> str:
    """Return the records contract spelling without coercing the Arrow type.

    Dictionary encodings must first be decoded to their scalar value type. Nested,
    binary and extension types have no records representation in this version.
    """
    if pa.types.is_date32(value):
        return "date32"
    if pa.types.is_date64(value):
        return "date64"
    if pa.types.is_float16(value):
        return "float16"
    if pa.types.is_float32(value):
        return "float32"
    if pa.types.is_float64(value):
        return "float64"
    if pa.types.is_decimal(value) and not 0 <= value.scale <= value.precision:
        raise DatasetContractError("Records decimal scale must fit its Parquet precision")
    supported = (
        pa.types.is_integer(value)
        or pa.types.is_boolean(value)
        or pa.types.is_string(value)
        or pa.types.is_large_string(value)
        or pa.types.is_decimal(value)
        or pa.types.is_time(value)
        or pa.types.is_timestamp(value)
        or pa.types.is_null(value)
    )
    if not supported:
        raise DatasetContractError(f"Unsupported records Arrow type: {value}")
    result = str(value)
    if len(result) > 200 or any(ord(character) < 32 for character in result):
        raise DatasetContractError("Arrow type exceeds records metadata limits")
    return result


def arrow_type(value: str) -> pa.DataType:
    """Parse the bounded scalar type vocabulary; arbitrary Arrow aliases are rejected."""
    if value in _ARROW_TYPES:
        return _ARROW_TYPES[value]
    aliases = {
        **{f"int{bits}": getattr(pa, f"int{bits}")() for bits in (8, 16, 32)},
        **{f"uint{bits}": getattr(pa, f"uint{bits}")() for bits in (8, 16, 32)},
        "float16": pa.float16(),
        "float32": pa.float32(),
        "date64": pa.date64(),
    }
    if value in aliases:
        return aliases[value]
    temporal = re.fullmatch(
        r"(time32|time64|timestamp)\[(s|ms|us|ns)(?:, tz=([^\]\r\n]+))?\]", value
    )
    decimal = re.fullmatch(r"decimal(32|64|128|256)\(([1-9]\d{0,2}), (\d{1,3})\)", value)
    try:
        if temporal:
            family, unit, timezone = temporal.groups()
            if timezone is not None and (family != "timestamp" or not timezone.strip()):
                raise ValueError("Only timestamp types support timezone metadata")
            return (
                pa.timestamp(unit, tz=timezone)
                if family == "timestamp"
                else getattr(pa, family)(unit)
            )
        if decimal:
            bits, precision, scale = decimal.groups()
            constructor = getattr(pa, f"decimal{bits}", None)
            if constructor is not None and int(scale) <= int(precision):
                return constructor(int(precision), int(scale))
    except (ValueError, TypeError, pa.ArrowException) as exc:
        raise DatasetContractError(f"Unsupported records Arrow type: {value}") from exc
    raise DatasetContractError(f"Unsupported records Arrow type: {value}")


def _validate_schema(specification: Table, schema: pa.Schema, rows: int) -> None:
    if rows != specification.rows:
        raise DatasetContractError("Arrow row count differs from the descriptor")
    if schema.names != [item.name for item in specification.columns]:
        raise DatasetContractError("Arrow columns or their order differ from the descriptor")
    for column in specification.columns:
        if schema.field(column.name).type != arrow_type(column.dtype):
            raise DatasetContractError(f"Arrow type differs for column {column.name}")


def _storage_schema(specification: Table, schema: pa.Schema, *, records: bool) -> pa.Schema:
    """Restore only documented, reversible Parquet scalar encodings for records."""
    if not records:
        return schema
    fields = []
    specifications = {column.name: column for column in specification.columns}
    for field in schema:
        if field.name not in specifications:
            fields.append(field)
            continue
        expected = arrow_type(specifications[field.name].dtype)
        actual = field.type
        encoded = (
            (pa.types.is_date64(expected) and pa.types.is_date32(actual))
            or (expected == pa.time32("s") and actual == pa.time32("ms"))
            or (
                pa.types.is_timestamp(expected)
                and pa.types.is_timestamp(actual)
                and expected.unit == "s"
                and actual.unit == "ms"
                and expected.tz == actual.tz
            )
        )
        fields.append(field.with_type(expected) if encoded else field)
    return pa.schema(fields, metadata=schema.metadata)


def _read_parquet(path: Path, specification: Table, *, records: bool) -> pa.Table:
    table = pq.read_table(path, use_threads=False, pre_buffer=False)
    schema = _storage_schema(specification, table.schema, records=records)
    # Safe casts reject a stored timestamp or time that would lose its finer
    # physical precision when restored to the declared scalar unit.
    return table.cast(schema, safe=True) if schema != table.schema else table


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DatasetContractError("Dataset descriptor contains duplicate JSON keys")
        value[key] = item
    return value


def _read_descriptor(directory: str | Path) -> tuple[Path, DatasetDescriptor]:
    root = Path(directory)
    descriptor_path = root / "dataset.json"
    if root.is_symlink() or descriptor_path.is_symlink():
        raise DatasetContractError("Dataset paths cannot be symlinks")
    if descriptor_path.stat().st_size > MAX_DESCRIPTOR_BYTES + 1:
        raise DatasetContractError("Dataset descriptor exceeds the 8 MiB limit")
    with descriptor_path.open("rb") as stream:
        document = json.loads(stream.read(MAX_DESCRIPTOR_BYTES + 2), object_pairs_hook=_json_pairs)
    descriptor = DatasetDescriptor.model_validate(document)
    for specification in descriptor.tables:
        path = root / specification.path
        if (root / "tables").is_symlink() or path.is_symlink():
            raise DatasetContractError("Dataset table paths cannot be symlinks")
    return root, descriptor


def _file_metadata(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def _validate_tables(descriptor: DatasetDescriptor, tables: Mapping[str, pa.Table]) -> None:
    if set(tables) != {item.name for item in descriptor.tables}:
        raise DatasetContractError("Arrow tables differ from the descriptor table inventory")
    for specification in descriptor.tables:
        table = tables[specification.name]
        if not isinstance(table, pa.Table):
            raise DatasetContractError("Dataset tables must be Arrow tables")
        _validate_schema(specification, table.schema, table.num_rows)
        table.validate(full=True)
        for column in specification.columns:
            if not column.nullable and table[column.name].null_count:
                raise DatasetContractError(f"Null values in required column {column.name}")
            if (
                descriptor.profile == "records"
                and pa.types.is_floating(table[column.name].type)
                and pc.any(pc.invert(pc.fill_null(pc.is_finite(table[column.name]), True))).as_py()
            ):
                raise DatasetContractError(f"Records column {column.name} must be finite or null")
    if descriptor.profile == "records":
        if descriptor.primary_key:
            records = tables["records"]
            # Arrow scalar values preserve decimal precision and temporal units;
            # avoid pandas conversion, string concatenation or lossy float keys.
            seen = set()
            columns = [records[name] for name in descriptor.primary_key]
            for position in range(records.num_rows):
                key = tuple(column[position] for column in columns)
                if key in seen:
                    raise DatasetContractError("Records primary key contains duplicate values")
                seen.add(key)
        return
    observations = tables["observations"]
    values = observations["value"]
    if pc.any(pc.invert(pc.fill_null(pc.is_finite(values), True))).as_py():
        raise DatasetContractError("Observation values must be finite or null")
    measures = pc.unique(observations["measure"])
    valid_measures = pa.array([item.id for item in descriptor.variables], type=pa.string())
    if pc.any(pc.invert(pc.is_in(measures, value_set=valid_measures))).as_py():
        raise DatasetContractError("Observation measure absent from variable definitions")
    for dimension in descriptor.dimensions:
        values = observations[dimension.column]
        if values.null_count:
            diagnostics = [
                item
                for item in descriptor.quality.diagnostics
                if item.code == "unknown_dimension" and item.column == dimension.column
            ]
            if not dimension.nullable or not any(
                item.count is None or item.count >= values.null_count for item in diagnostics
            ):
                raise DatasetContractError("Null dimension codes require unknown diagnostics")
        actual = pc.drop_null(pc.unique(values))
        known = pa.array([item.code for item in dimension.codes], type=pa.string())
        if pc.any(pc.invert(pc.is_in(actual, value_set=known))).as_py():
            raise DatasetContractError(f"Observation code absent from dimension {dimension.id}")


class Dataset:
    """A validated descriptor and immutable Arrow tables, optionally bound to a snapshot."""

    def __init__(
        self,
        descriptor: DatasetDescriptor | Mapping[str, Any],
        tables: Mapping[str, pa.Table],
        snapshot_id: str | None = None,
    ):
        if snapshot_id is not None and (
            not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_id)
        ):
            raise DatasetContractError("Snapshot binding must be a full SHA-256 identifier")
        try:
            document = (
                descriptor.model_dump()
                if isinstance(descriptor, DatasetDescriptor)
                else dict(descriptor)
            )
            parsed = DatasetDescriptor.model_validate(deepcopy(document))
            _validate_tables(parsed, tables)
        except (ValueError, TypeError, pa.ArrowException) as exc:
            if isinstance(exc, DatasetContractError):
                raise
            raise DatasetContractError(f"Invalid dataset contract: {exc}") from exc
        self._descriptor = parsed
        self._tables = dict(tables)
        self.snapshot_id = snapshot_id

    @classmethod
    def from_tables(
        cls,
        descriptor: DatasetDescriptor | Mapping[str, Any],
        tables: Mapping[str, pa.Table],
        *,
        snapshot_id: str | None = None,
    ) -> Dataset:
        return cls(descriptor, tables, snapshot_id)

    @property
    def descriptor(self) -> DatasetDescriptor:
        return self._descriptor.model_copy(deep=True)

    @property
    def contract(self) -> dict[str, Any]:
        return self._descriptor.model_dump()

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._descriptor.tables)

    @property
    def default_table(self) -> str:
        return self._descriptor.profile

    def to_arrow(self, table: str | None = None) -> pa.Table:
        table = self.default_table if table is None else table
        if not isinstance(table, str):
            raise DatasetContractError("Dataset table name must be a string")
        try:
            return self._tables[table]
        except KeyError as exc:
            raise DatasetContractError(f"Unknown dataset table: {table}") from exc

    def to_pandas(self, table: str | None = None):
        arrow = self.to_arrow(table)
        if self._descriptor.profile == "records":
            # Arrow-backed dtypes retain nullable integers, wide decimals and
            # timestamps with native units/timezones in user-facing dataframes.
            import pandas as pd

            return arrow.to_pandas(types_mapper=pd.ArrowDtype)
        return arrow.to_pandas()

    def write(self, directory: str | Path) -> dict[str, Any]:
        """Write canonical descriptor and Parquet tables without overwriting existing products."""
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        paths = [root / "dataset.json"] + [root / item.path for item in self._descriptor.tables]
        if (
            root.is_symlink()
            or (root / "tables").is_symlink()
            or any(path.exists() or path.is_symlink() for path in paths)
        ):
            raise DatasetContractError("Dataset destination contains existing files or symlinks")
        (root / "tables").mkdir(exist_ok=True)
        created = []
        temporary = None
        try:
            for specification in self._descriptor.tables:
                path = root / specification.path
                created.append(path)
                pq.write_table(self._tables[specification.name], path, compression="zstd")
                if self._descriptor.profile == "records":
                    restored = _read_parquet(path, specification, records=True)
                    if not restored.equals(self._tables[specification.name]):
                        raise DatasetContractError(
                            "Parquet encoding would lose records values or types"
                        )
            fd, temporary = tempfile.mkstemp(dir=root, prefix=".dataset-")
            with os.fdopen(fd, "wb") as stream:
                stream.write(_canonical(self.contract) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / "dataset.json")
            created.append(root / "dataset.json")
        except (OSError, pa.ArrowException, DatasetContractError) as exc:
            for path in created:
                path.unlink(missing_ok=True)
            if isinstance(exc, DatasetContractError):
                raise
            raise DatasetContractError("Cannot persist dataset contract") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return {path.relative_to(root).as_posix(): _file_metadata(path) for path in paths}

    @classmethod
    def inspect(cls, directory: str | Path) -> DatasetDescriptor:
        """Validate descriptor and Parquet schemas/counts without reading all records.

        This checks storage structure. Use ``read`` for value, code and null semantics,
        and Store verification for surrounding immutable-file checksums.
        """
        try:
            root, descriptor = _read_descriptor(directory)
            for specification in descriptor.tables:
                with pq.ParquetFile(root / specification.path) as parquet:
                    schema = _storage_schema(
                        specification,
                        parquet.schema_arrow,
                        records=descriptor.profile == "records",
                    )
                    _validate_schema(specification, schema, parquet.metadata.num_rows)
            return descriptor
        except (OSError, ValueError, TypeError, pa.ArrowException) as exc:
            if isinstance(exc, DatasetContractError):
                raise
            raise DatasetContractError(f"Cannot inspect dataset contract: {exc}") from exc

    @classmethod
    def read(cls, directory: str | Path, snapshot_id: str | None = None) -> Dataset:
        """Read a contract, validating paths, table types, code lists and row counts.

        Store callers separately verify immutable manifest checksums. This reader checks
        semantic integrity and does not imply cryptographic verification of loose files.
        """
        try:
            root, descriptor = _read_descriptor(directory)
            tables = {}
            for specification in descriptor.tables:
                tables[specification.name] = _read_parquet(
                    root / specification.path,
                    specification,
                    records=descriptor.profile == "records",
                )
            return cls.from_tables(descriptor, tables, snapshot_id=snapshot_id)
        except (OSError, ValueError, TypeError, pa.ArrowException) as exc:
            if isinstance(exc, DatasetContractError):
                raise
            raise DatasetContractError(f"Cannot read dataset contract: {exc}") from exc
