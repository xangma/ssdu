"""Pure records-contract normalization for retained MHCLG tabular resources."""

import pyarrow as pa

from ssdu.dataset_contract import Dataset, DatasetContractError, arrow_dtype
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.records import normalize_records

TRANSFORM_ID = "ssdu.mhclg.records"
TRANSFORM_VERSION = "1.0.0"


def normalize(acquisition: Acquisition, request: Request | None = None, raw_frame=None):
    """Preserve declared records and original CSV lexemes without inferring measures."""
    if acquisition.provider != "mhclg":
        raise RetrievalError("MHCLG normalizer received a different provider")
    if raw_frame is None:
        raise RetrievalError("MHCLG normalization requires its retained original source cells")
    if len(raw_frame) != len(acquisition.frame):
        raise RetrievalError("MHCLG source and records row counts differ")
    records = normalize_records(acquisition, request=request)
    try:
        source = pa.Table.from_pandas(raw_frame, preserve_index=False).replace_schema_metadata(None)
        descriptor = records.contract
        descriptor["transform"] = {"id": TRANSFORM_ID, "version": TRANSFORM_VERSION}
        source_spec = next(table for table in descriptor["tables"] if table["name"] == "source")
        source_spec["columns"] = [
            {
                "name": field.name,
                "dtype": arrow_dtype(field.type),
                "role": "attribute",
                "nullable": True,
            }
            for field in source.schema
        ]
        return Dataset.from_tables(
            descriptor, {"records": records.to_arrow("records"), "source": source}
        )
    except (DatasetContractError, pa.ArrowException, ValueError, TypeError) as exc:
        raise RetrievalError(f"MHCLG table does not satisfy the records contract: {exc}") from exc
