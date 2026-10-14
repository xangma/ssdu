"""Offline imports with exact-byte provenance and explicit local metadata."""

import csv
import hashlib
import math
import os
import re
import stat
import threading
from io import BytesIO, StringIO
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import Field, StrictBool, StringConstraints, field_validator, model_validator

from ssdu.contracts import StrictModel
from ssdu.http import Resource
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.registry import Registry
from ssdu.store import Store, canonical

MAX_BYTES = 25_000_000
MAX_DECODED_BYTES = 100_000_000
MAX_COLUMNS = 1000
ColumnName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
ColumnType = Literal["string", "int64", "float64", "boolean"]
_CSV_LOCK = threading.Lock()


class LocalColumn(StrictModel):
    label: Annotated[str, StringConstraints(max_length=500)] | None = None
    definition: Annotated[str, StringConstraints(max_length=10_000)] | None = None
    unit: Annotated[str, StringConstraints(max_length=200)] | None = None
    type: ColumnType | None = None
    nullable: StrictBool | None = None
    role: Literal["attribute", "identifier"] | None = None
    missing_values: dict[
        Annotated[str, StringConstraints(max_length=2000)],
        Annotated[str, StringConstraints(min_length=1, max_length=2000)],
    ] = Field(default_factory=dict, max_length=100)


class LocalImportSpec(StrictModel):
    dataset_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]
    title: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    description: Annotated[str, StringConstraints(max_length=10_000)] = ""
    licence: Annotated[str, StringConstraints(min_length=1, max_length=2000)] = (
        "Unspecified local rights"
    )
    citation: Annotated[str, StringConstraints(max_length=10_000)] = ""
    column_types: dict[ColumnName, ColumnType] = Field(default_factory=dict, max_length=1000)
    columns: dict[ColumnName, LocalColumn] = Field(default_factory=dict, max_length=1000)
    primary_key: Annotated[list[ColumnName], Field(max_length=100)] = Field(default_factory=list)
    row_grain: Annotated[str, StringConstraints(min_length=1, max_length=10_000)] | None = None
    null_values: Annotated[
        list[Annotated[str, StringConstraints(max_length=2000)]], Field(max_length=100)
    ] = Field(default_factory=lambda: [""])
    max_rows: Annotated[int, Field(ge=1, le=100_000)] = 100_000

    @field_validator("title", "licence")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value

    @model_validator(mode="after")
    def consistent_declarations(self):
        if len(self.primary_key) != len(set(self.primary_key)):
            raise ValueError("Primary key columns must be unique")
        if self.row_grain is not None and not self.row_grain.strip():
            raise ValueError("Row grain must not be blank")
        for name, column in self.columns.items():
            if column.type is not None and self.column_types.get(name, column.type) != column.type:
                raise ValueError(f"Conflicting type declarations for column {name!r}")
            if name in self.primary_key and (column.nullable is True or column.role == "attribute"):
                raise ValueError("Primary key columns must be non-nullable identifiers")
        return self

    def declared_types(self):
        return {
            **self.column_types,
            **{name: column.type for name, column in self.columns.items() if column.type},
        }


def _validate_columns(names, spec):
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise RetrievalError("Import requires non-empty string column names")
    if len(names) != len(set(names)):
        raise RetrievalError("Import contains duplicate column names")
    if len(names) > MAX_COLUMNS:
        raise RetrievalError(f"Import exceeds the {MAX_COLUMNS} column limit")
    unknown = sorted(
        (set(spec.column_types) | set(spec.columns) | set(spec.primary_key)) - set(names)
    )
    if unknown:
        raise RetrievalError(f"Import specification names unknown columns: {', '.join(unknown)}")


def _csv_frame(body, spec):
    try:
        text = body.decode("utf-8-sig")
        # The file byte limit also bounds individual fields. Restore the process
        # setting afterwards; serialize importer changes to the csv module limit.
        with _CSV_LOCK:
            previous_limit = csv.field_size_limit()
            csv.field_size_limit(MAX_BYTES)
            try:
                reader = csv.reader(StringIO(text, newline=""), strict=True)
                names = next((row for row in reader if row), [])
                _validate_columns(names, spec)
                rows = []
                for row in reader:
                    if not row:
                        continue
                    if len(row) != len(names):
                        raise RetrievalError(
                            f"CSV record at line {reader.line_num} does not match the header"
                        )
                    rows.append(row)
                    if len(rows) > spec.max_rows:
                        raise RetrievalError(
                            f"Import exceeds max_rows={spec.max_rows}; no truncation"
                        )
            finally:
                csv.field_size_limit(previous_limit)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise RetrievalError("Import requires a valid UTF-8 CSV") from exc
    frame = pd.DataFrame(rows, columns=names, dtype="string")
    for name in names:
        frame[name] = frame[name].mask(frame[name].isin(spec.null_values), pd.NA)
    for name, dtype in spec.declared_types().items():
        if dtype == "string":
            continue
        values = []
        try:
            for value in frame[name]:
                if value is pd.NA:
                    values.append(pd.NA)
                elif dtype == "int64":
                    if not re.fullmatch(r"[+-]?\d+", value):
                        raise ValueError("Not an integer")
                    number = int(value)
                    if not -(2**63) <= number < 2**63:
                        raise ValueError("Integer outside int64 range")
                    values.append(number)
                elif dtype == "float64":
                    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
                        raise ValueError("Not a number")
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("Non-finite number")
                    values.append(number)
                else:
                    token = value.casefold()
                    if token not in {"true", "false", "1", "0"}:
                        raise ValueError("Not a boolean")
                    values.append(token in {"true", "1"})
            target = {"int64": "Int64", "float64": "Float64", "boolean": "boolean"}[dtype]
            frame[name] = pd.Series(values, dtype=target)
        except (ValueError, TypeError, OverflowError) as exc:
            raise RetrievalError(f"Column {name!r} contains invalid {dtype} values") from exc
    return frame


def _parquet_frame(body, spec):
    try:
        source = pq.ParquetFile(BytesIO(body))
        _validate_columns(source.schema_arrow.names, spec)
        if source.metadata.num_rows > spec.max_rows:
            raise RetrievalError(f"Import exceeds max_rows={spec.max_rows}; no truncation")
        uncompressed = sum(
            source.metadata.row_group(group).column(column).total_uncompressed_size
            for group in range(source.metadata.num_row_groups)
            for column in range(source.metadata.num_columns)
        )
        if uncompressed > MAX_DECODED_BYTES:
            raise RetrievalError("Parquet footer exceeds the 100 MB uncompressed-size budget")
        for field in source.schema_arrow:
            native = field.type.value_type if pa.types.is_dictionary(field.type) else field.type
            supported = (
                pa.types.is_integer(native)
                or pa.types.is_floating(native)
                or pa.types.is_boolean(native)
                or pa.types.is_string(native)
                or pa.types.is_large_string(native)
                or pa.types.is_decimal(native)
                or pa.types.is_date(native)
                or pa.types.is_time(native)
                or pa.types.is_timestamp(native)
                or pa.types.is_null(native)
            )
            if not supported:
                raise RetrievalError(
                    f"Parquet column {field.name!r} has unsupported type {field.type}"
                )
        table = source.read()
        if table.nbytes > MAX_DECODED_BYTES:
            raise RetrievalError("Decoded Parquet exceeds the 100 MB budget")
        for field in table.schema:
            native = field.type.value_type if pa.types.is_dictionary(field.type) else field.type
            if pa.types.is_floating(native):
                column = table.column(field.name)
                if pa.types.is_dictionary(field.type):
                    column = pc.cast(column, native)
                if pc.all(pc.fill_null(pc.is_finite(column), True)).as_py() is False:
                    raise RetrievalError(
                        f"Parquet column {field.name!r} contains non-finite floats"
                    )
        targets = {
            "string": pa.string(),
            "int64": pa.int64(),
            "float64": pa.float64(),
            "boolean": pa.bool_(),
        }
        for name, dtype in spec.declared_types().items():
            position = table.schema.get_field_index(name)
            native = table.schema.field(position).type
            numeric = pa.types.is_integer(native) or pa.types.is_floating(native)
            string = pa.types.is_string(native) or pa.types.is_large_string(native)
            if pa.types.is_dictionary(native):
                string = pa.types.is_string(native.value_type)
            compatible = (
                (dtype in {"int64", "float64"} and numeric)
                or (dtype == "string" and string)
                or (dtype == "boolean" and pa.types.is_boolean(native))
            )
            if not compatible:
                raise RetrievalError(
                    f"Parquet column {name!r} has incompatible type {native} for {dtype}"
                )
            try:
                converted = pc.cast(table.column(position), targets[dtype], safe=True)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
                raise RetrievalError(
                    f"Parquet column {name!r} cannot be losslessly cast to {dtype}"
                ) from exc
            table = table.set_column(position, name, converted)
        # Arrow-backed dtypes retain integer widths, native nulls and temporal
        # types. Physical index columns remain data rather than being discarded.
        return table.to_pandas(types_mapper=pd.ArrowDtype, ignore_metadata=True)
    except (pa.ArrowException, OSError, ValueError) as exc:
        raise RetrievalError("Import requires a valid Parquet file") from exc


def import_local(store: Store, path, spec: LocalImportSpec | dict) -> dict:
    """Publish and register a bounded local file without contacting providers."""
    spec = LocalImportSpec.model_validate(spec)
    path = Path(path)
    format_name = path.suffix.casefold().lstrip(".")
    if format_name not in {"csv", "parquet"}:
        raise RetrievalError("Local import supports .csv and .parquet files")
    try:
        if not path.is_file():
            raise RetrievalError("Local import requires a regular file")
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RetrievalError("Local import requires a regular file")
            body = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        raise RetrievalError("Local import file cannot be read") from exc
    if len(body) > MAX_BYTES:
        raise RetrievalError("Local import exceeds the 25 MB raw-file limit")
    frame = _csv_frame(body, spec) if format_name == "csv" else _parquet_frame(body, spec)
    if frame.empty:
        raise RetrievalError("Local import requires at least one data row")
    digest = hashlib.sha256(body).hexdigest()
    source_url = f"urn:ssdu:local:sha256:{digest}"
    options = spec.model_dump(mode="json")
    params = {
        "source_sha256": digest,
        "format": format_name,
        "original_filename": path.name,
        "import_spec": options,
    }
    request = Request("local", spec.dataset_id, params=params, max_rows=spec.max_rows)
    metadata = {
        **options,
        "original_filename": path.name,
        "format": format_name,
        "source_sha256": digest,
        "raw_bytes": len(body),
        "null_policy": {
            "csv": "Exact text tokens in null_values; other text including NA is preserved",
            "parquet": "Native nulls retained; CSV null_values are not applied",
        },
        "csv_encoding": "utf-8-sig" if format_name == "csv" else None,
        "boolean_tokens": {"true": True, "false": False, "1": True, "0": False},
        "typing": "CSV defaults to strings; Parquet keeps native types; casts must be explicit",
        "access": "local",
        "complete_selection": True,
    }
    acquisition = Acquisition(
        "local",
        spec.dataset_id,
        spec.title,
        frame,
        metadata,
        {"source_sha256": digest, "format": format_name},
        spec.licence,
        spec.citation,
        source_url,
    )
    from ssdu.records import implementation_paths, normalize_records

    acquisition.dataset = normalize_records(acquisition, request=request)
    resource = Resource(
        body=body,
        url=source_url,
        method="LOCAL_IMPORT",
        request_body=None,
        content_type="text/csv" if format_name == "csv" else "application/vnd.apache.parquet",
        headers={},
    )
    manifest = store._publish(
        request,
        hashlib.sha256(canonical(request.to_dict())).hexdigest(),
        acquisition,
        [resource],
        kind="local",
        transformation_version="local-import-v2",
        implementation_paths=implementation_paths(),
    )
    Registry(store.root).register(store, manifest, make_current=True)
    return manifest
