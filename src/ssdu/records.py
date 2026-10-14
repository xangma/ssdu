"""Pure, explicit local-table interpretation and offline historical migration."""

from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from ssdu.models import Acquisition, Request, RetrievalError

TRANSFORM_ID = "ssdu.local.records"
TRANSFORM_VERSION = "1.0.0"


def implementation_paths():
    """Include each local interpretation dependency in immutable cache identities."""
    return tuple(
        Path(__file__).with_name(name)
        for name in (
            "records.py",
            "local_import.py",
            "dataset_contract.py",
            "contracts.py",
            "models.py",
            "store.py",
        )
    )


def normalize_records(acquisition: Acquisition, *, request: Request | None = None):
    """Describe source fields without inferring geography, measures or missing tokens."""
    from ssdu.dataset_contract import (
        RECORDS_CONTRACT_VERSION,
        Dataset,
        DatasetContractError,
        arrow_dtype,
    )

    metadata = acquisition.metadata
    primary_key = metadata.get("primary_key", [])
    try:
        table = pa.Table.from_pandas(
            acquisition.frame, preserve_index=False
        ).replace_schema_metadata(None)
        # Dictionary encoding is a storage choice; canonical records retain the
        # exact native values, nulls and logical scalar type instead.
        for position, field in enumerate(table.schema):
            if pa.types.is_dictionary(field.type):
                table = table.set_column(
                    position, field.name, pc.cast(table.column(position), field.type.value_type)
                )
        columns = []
        for field in table.schema:
            declared = metadata.get("columns", {}).get(field.name, {})
            declared = declared if isinstance(declared, dict) else declared.model_dump()
            is_key = field.name in primary_key
            nullable = declared.get("nullable")
            columns.append(
                {
                    "name": field.name,
                    "dtype": arrow_dtype(field.type),
                    "role": declared.get("role") or ("identifier" if is_key else "attribute"),
                    "nullable": not is_key if nullable is None else nullable,
                    **{
                        name: declared.get(name)
                        for name in ("label", "definition", "unit")
                        if declared.get(name) is not None
                    },
                    "missing_values": declared.get("missing_values", {}),
                }
            )
        descriptor = {
            "contract_version": RECORDS_CONTRACT_VERSION,
            "profile": "records",
            "dataset_id": f"{acquisition.provider}:{acquisition.dataset_id}",
            "title": acquisition.title,
            "source": {
                "provider": acquisition.provider,
                "dataset_id": acquisition.dataset_id,
                "url": acquisition.source_url,
                "licence": acquisition.licence,
                "citation": acquisition.citation,
                "release": acquisition.release,
                "effective_request": request.to_dict() if request else {},
            },
            "transform": {"id": TRANSFORM_ID, "version": TRANSFORM_VERSION},
            "primary_key": primary_key,
            "row_grain": metadata.get("row_grain"),
            "dimensions": [],
            "variables": [],
            "tables": [
                {
                    "name": name,
                    "path": f"tables/{name}.parquet",
                    "rows": table.num_rows,
                    "columns": columns,
                }
                for name in ("records", "source")
            ],
            "quality": {
                "source_rows": table.num_rows,
                "observation_rows": 0,
                "records_rows": table.num_rows,
                "completeness": "complete",
                "diagnostics": [],
            },
        }
        return Dataset.from_tables(descriptor, {"records": table, "source": table})
    except (DatasetContractError, pa.ArrowException, ValueError, TypeError) as exc:
        raise RetrievalError(f"Local import does not satisfy the records contract: {exc}") from exc


def retained_frame(manifest, path):
    """Reparse verified original bytes with the historical import's explicit policy."""
    from ssdu.local_import import MAX_BYTES, LocalImportSpec, _csv_frame, _parquet_frame

    params = manifest["request"]["params"]
    try:
        spec = LocalImportSpec.model_validate(params["import_spec"])
        candidates = [
            resource
            for resource in manifest["resources"]
            if resource["method"] == "LOCAL_IMPORT"
            and manifest["files"][resource["file"]]["sha256"] == params["source_sha256"]
        ]
        if len(candidates) != 1:
            raise ValueError("Expected exactly one retained original local file")
        raw_path = path / candidates[0]["file"]
        if raw_path.stat().st_size > MAX_BYTES:
            raise RetrievalError("Historical local file exceeds the 25 MB import limit")
        body = raw_path.read_bytes()
        if params["format"] == "csv":
            return _csv_frame(body, spec)
        if params["format"] == "parquet":
            return _parquet_frame(body, spec)
        raise ValueError("Unsupported historical local file format")
    except (KeyError, TypeError, ValueError) as exc:
        raise RetrievalError(
            "Historical local import lacks a valid retained import policy"
        ) from exc
