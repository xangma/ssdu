"""Invented records exercise exact types, explicit semantics and offline persistence."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ssdu.dataset_contract import (
    CONTRACT_VERSION,
    RECORDS_CONTRACT_VERSION,
    Dataset,
    DatasetContractError,
    DatasetDescriptor,
    arrow_dtype,
    arrow_type,
)


def records_dataset(table=None, *, primary_key=None, source=False):
    """Build deliberately invented rows without provider-derived metadata."""
    if table is None:
        table = pa.table(
            {
                "code": pa.array(["001", "002", "003"], type=pa.string()),
                "count": pa.array([0, None, 2**63 + 11], type=pa.uint64()),
                "flag": pa.array([True, False, None], type=pa.bool_()),
                "status": pa.array([None, ":", "missing"], type=pa.string()),
            }
        )
    primary_key = [] if primary_key is None else primary_key
    columns = [
        {
            "name": field.name,
            "dtype": arrow_dtype(field.type),
            "role": "identifier" if field.name in primary_key else "attribute",
            "nullable": field.name not in primary_key,
        }
        for field in table.schema
    ]
    descriptor = {
        "contract_version": RECORDS_CONTRACT_VERSION,
        "profile": "records",
        "dataset_id": "local:invented-records",
        "title": "Invented records",
        "source": {
            "provider": "local",
            "dataset_id": "invented-records",
            "url": "urn:ssdu:synthetic:test-records",
            "licence": "MIT; wholly invented test fixture",
            "citation": "Invented records test fixture",
            "raw_inputs": [{"path": "raw/0.csv", "sha256": "a" * 64, "bytes": 11}],
        },
        "transform": {"id": "ssdu.local.records", "version": "1.0.0"},
        "tables": [
            {
                "name": "records",
                "path": "tables/records.parquet",
                "rows": table.num_rows,
                "columns": columns,
            }
        ],
        "quality": {
            "source_rows": table.num_rows,
            "observation_rows": 0,
            "records_rows": table.num_rows,
        },
        "primary_key": primary_key,
    }
    tables = {"records": table}
    if source:
        descriptor["tables"].append(
            {
                "name": "source",
                "path": "tables/source.parquet",
                "rows": table.num_rows,
                "columns": deepcopy(columns),
            }
        )
        tables["source"] = table
    return descriptor, tables


def test_records_roundtrip_retains_codes_nulls_keys_and_explicit_semantics(tmp_path):
    descriptor, tables = records_dataset(primary_key=["code"], source=True)
    descriptor["row_grain"] = "One invented record per code; no geographic meaning implied"
    descriptor["tables"][0]["columns"][0].update(
        label="Record code", definition="A zero-padded invented identifier"
    )
    descriptor["tables"][0]["columns"][1].update(unit="invented items")
    descriptor["tables"][0]["columns"][3].update(
        missing_values={":": "Suppressed", "missing": "Not collected"}
    )
    dataset = Dataset.from_tables(descriptor, tables)
    files = dataset.write(tmp_path)
    reopened = Dataset.read(tmp_path, snapshot_id="b" * 64)
    assert Dataset.inspect(tmp_path).model_dump() == dataset.contract
    assert reopened.contract == dataset.contract
    assert reopened.table_names == ("records", "source")
    assert reopened.default_table == "records"
    assert reopened.to_arrow().equals(tables["records"])
    assert reopened.to_arrow("source").equals(tables["source"])
    assert reopened.to_pandas()["code"].tolist() == ["001", "002", "003"]
    assert reopened.to_pandas()["count"].dtype.pyarrow_dtype == pa.uint64()
    assert reopened.to_pandas()["count"].tolist()[2] == 2**63 + 11
    assert reopened.to_arrow()["count"].to_pylist() == [0, None, 2**63 + 11]
    assert reopened.descriptor.dimensions == []
    assert reopened.descriptor.variables == []
    assert reopened.contract["coverage"]["geography"] == {"status": "unknown"}
    assert set(files) == {"dataset.json", "tables/records.parquet", "tables/source.parquet"}
    assert "b" * 64 not in (tmp_path / "dataset.json").read_text()
    for table in ("observations", "other", "../records"):
        with pytest.raises(DatasetContractError, match="Unknown dataset table"):
            reopened.to_arrow(table)


NATIVE_SCALARS = [
    *[(getattr(pa, f"int{bits}")(), [-(2 ** (bits - 1)), None]) for bits in (8, 16, 32, 64)],
    *[(getattr(pa, f"uint{bits}")(), [2**bits - 1, None]) for bits in (8, 16, 32, 64)],
    (pa.float16(), [1.5, None]),
    (pa.float32(), [1.5, None]),
    (pa.float64(), [1.5, None]),
    (pa.bool_(), [True, None]),
    (pa.string(), ["001", None]),
    (pa.large_string(), ["001", None]),
    (pa.null(), [None, None]),
    (pa.decimal128(30, 12), [Decimal("123456789012345678.123456789012"), None]),
    (
        pa.decimal256(60, 30),
        [Decimal("123456789012345678901234567890.123456789012345678901234567890"), None],
    ),
    (pa.date32(), [date(2026, 10, 14), None]),
    (pa.date64(), [date(2026, 10, 14), None]),
    (pa.time32("s"), [time(12, 34, 56), None]),
    (pa.time32("ms"), [time(12, 34, 56, 123000), None]),
    (pa.time64("us"), [time(12, 34, 56, 123456), None]),
    (pa.time64("ns"), [1_234_567_890_123, None]),
    *[(pa.timestamp(unit), [1_234_567_890, None]) for unit in ("s", "ms", "us", "ns")],
    (pa.timestamp("ns", "Europe/London"), [1_234_567_890_123_456_789, None]),
    (pa.timestamp("s", "UTC"), [datetime(2026, 10, 14, tzinfo=UTC), None]),
]
for bits, precision in ((32, 7), (64, 16)):
    constructor = getattr(pa, f"decimal{bits}", None)
    if constructor is not None:
        NATIVE_SCALARS.append((constructor(precision, 2), [Decimal("123.45"), None]))


@pytest.mark.parametrize("dtype,values", NATIVE_SCALARS, ids=[str(t) for t, _ in NATIVE_SCALARS])
def test_records_native_scalar_roundtrip_without_precision_loss(tmp_path, dtype, values):
    table = pa.table({"native": pa.array(values, type=dtype)})
    descriptor, tables = records_dataset(table)
    dataset = Dataset.from_tables(descriptor, tables)
    dataset.write(tmp_path)
    inspected = Dataset.inspect(tmp_path)
    restored = Dataset.read(tmp_path)
    assert inspected.tables[0].columns[0].dtype == arrow_dtype(dtype)
    assert arrow_type(arrow_dtype(dtype)) == dtype
    assert restored.to_arrow().schema == table.schema
    assert restored.to_arrow().equals(table)
    assert restored.to_pandas()["native"].dtype.pyarrow_dtype == dtype


@pytest.mark.parametrize("key", [[], ["group"], ["group", "code"]])
def test_composite_primary_key_validates_actual_typed_values(key):
    table = pa.table({"group": ["A", "A", "B"], "code": ["001", "002", "001"]})
    descriptor, tables = records_dataset(table, primary_key=key)
    if key == ["group"]:
        with pytest.raises(DatasetContractError, match="duplicate"):
            Dataset.from_tables(descriptor, tables)
    else:
        assert Dataset.from_tables(descriptor, tables).descriptor.primary_key == key


@pytest.mark.parametrize("dtype", [pa.time64("ns"), pa.timestamp("ns"), pa.uint64()])
def test_keys_do_not_round_or_stringify_native_values(dtype):
    table = pa.table({"key": pa.array([1, 2], type=dtype)})
    descriptor, tables = records_dataset(table, primary_key=["key"])
    assert Dataset.from_tables(descriptor, tables).to_arrow().equals(table)
    tables["records"] = pa.table({"key": pa.array([1, 1], type=dtype)})
    with pytest.raises(DatasetContractError, match="duplicate"):
        Dataset.from_tables(descriptor, tables)


def test_positive_and_negative_zero_are_duplicate_keys():
    table = pa.table({"key": [0.0, -0.0]})
    descriptor, tables = records_dataset(table, primary_key=["key"])
    with pytest.raises(DatasetContractError, match="duplicate"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("mutation", ["null", "nullable", "unknown", "repeated"])
def test_invalid_primary_keys_fail_closed(mutation):
    descriptor, tables = records_dataset(primary_key=["code"])
    if mutation == "null":
        tables["records"] = tables["records"].set_column(0, "code", pa.array(["001", None, "003"]))
    elif mutation == "nullable":
        descriptor["tables"][0]["columns"][0]["nullable"] = True
    elif mutation == "unknown":
        descriptor["primary_key"] = ["not_a_column"]
    else:
        descriptor["primary_key"] = ["code", "code"]
    with pytest.raises(DatasetContractError, match="required column|Primary key"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("dtype", [pa.float16(), pa.float32(), pa.float64()])
def test_nonfinite_records_rejected(value, dtype):
    descriptor, tables = records_dataset(pa.table({"number": pa.array([value, None], type=dtype)}))
    with pytest.raises(DatasetContractError, match="finite"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("mutation", ["type", "order", "extra", "rows", "quality", "source"])
def test_records_schema_and_counts_are_enforced(mutation):
    descriptor, tables = records_dataset(source=True)
    table = tables["records"]
    if mutation == "type":
        tables["records"] = table.set_column(0, "code", pa.array([1, 2, 3], type=pa.int64()))
    elif mutation == "order":
        tables["records"] = table.select(list(reversed(table.column_names)))
    elif mutation == "extra":
        tables["records"] = table.append_column("new", pa.array(["x", "y", "z"]))
    elif mutation == "rows":
        tables["records"] = table.slice(0, 2)
    elif mutation == "quality":
        descriptor["quality"]["records_rows"] = 5
    else:
        tables.pop("source")
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("version", ["1.0.0", "1.2.0", "2.0.0", None, 1])
def test_records_reject_unsupported_profile_versions(version):
    descriptor, tables = records_dataset()
    descriptor["contract_version"] = version
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize(
    "dtype",
    [
        pa.binary(),
        pa.list_(pa.string()),
        pa.struct([("x", pa.int64())]),
        pa.dictionary(pa.int8(), pa.string()),
    ],
)
def test_unsupported_nested_binary_and_dictionary_types_are_explicit(dtype):
    with pytest.raises(DatasetContractError, match="Unsupported"):
        arrow_dtype(dtype)


@pytest.mark.parametrize(
    "dtype",
    [
        "int128",
        "double",
        "string[invalid]",
        "timestamp[weeks]",
        "time64[ms]",
        "decimal128(39, 0)",
        "decimal128(8, -2)",
        "decimal128(8, 9)",
    ],
)
def test_dtype_parser_is_a_bounded_vocabulary(dtype):
    descriptor, tables = records_dataset()
    descriptor["tables"][0]["columns"][0]["dtype"] = dtype
    with pytest.raises(DatasetContractError, match="Unsupported"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("name", ["../records", "source", "/records", "records.parquet"])
def test_records_table_path_is_canonical(name):
    descriptor, tables = records_dataset()
    descriptor["tables"][0]["path"] = f"tables/{name}.parquet"
    with pytest.raises(DatasetContractError, match="canonical"):
        Dataset.from_tables(descriptor, tables)


def test_records_symlink_and_overwrite_protections(tmp_path):
    descriptor, tables = records_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    destination = tmp_path / "destination"
    dataset.write(destination)
    with pytest.raises(DatasetContractError, match="existing"):
        dataset.write(destination)
    linked = tmp_path / "linked"
    linked.symlink_to(destination, target_is_directory=True)
    for operation in (dataset.write, Dataset.inspect, Dataset.read):
        with pytest.raises(DatasetContractError, match="symlink"):
            operation(linked)
    path = destination / "tables" / "records.parquet"
    original = tmp_path / "original.parquet"
    path.rename(original)
    path.symlink_to(original)
    for operation in (Dataset.inspect, Dataset.read):
        with pytest.raises(DatasetContractError, match="symlink"):
            operation(destination)


def test_corrupted_physical_temporal_values_cannot_be_truncated_on_read(tmp_path):
    descriptor, tables = records_dataset(
        pa.table({"when": pa.array([1, None], type=pa.time32("s"))})
    )
    Dataset.from_tables(descriptor, tables).write(tmp_path)
    pq.write_table(
        pa.table({"when": pa.array([1001, None], type=pa.time32("ms"))}),
        tmp_path / "tables/records.parquet",
    )
    Dataset.inspect(tmp_path)  # Schema inspection intentionally does not read values.
    with pytest.raises(DatasetContractError):
        Dataset.read(tmp_path)


def test_date64_nonmidnight_values_fail_without_silent_parquet_loss(tmp_path):
    descriptor, tables = records_dataset(pa.table({"day": pa.array([1], type=pa.date64())}))
    with pytest.raises(DatasetContractError, match="whole number of days"):
        Dataset.from_tables(descriptor, tables)
    assert not (tmp_path / "dataset.json").exists()
    assert not (tmp_path / "tables/records.parquet").exists()


def test_legacy_observation_descriptor_dump_remains_identical(tmp_path):
    from tests.test_dataset_contract import fixture_dataset

    descriptor, tables = fixture_dataset()
    baseline = Dataset.from_tables(descriptor, tables)
    document = baseline.contract
    assert document["contract_version"] == CONTRACT_VERSION == "1.0.0"
    assert "primary_key" not in document
    assert "row_grain" not in document
    assert "records_rows" not in document["quality"]
    assert all(
        set(column) == {"name", "dtype", "role", "nullable"}
        for table in document["tables"]
        for column in table["columns"]
    )
    baseline.write(tmp_path)
    assert json.loads((tmp_path / "dataset.json").read_text()) == document
    assert DatasetDescriptor.model_validate(document).model_dump() == document
    assert Dataset.read(tmp_path).contract == document


def test_explicit_unknown_grain_and_empty_keys_do_not_infer_semantics():
    descriptor, tables = records_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    assert dataset.contract["primary_key"] == []
    assert dataset.contract["row_grain"] is None
    assert dataset.contract["dimensions"] == []
    assert dataset.contract["variables"] == []


@pytest.mark.parametrize("table", [[], {}, 1, True])
def test_dataset_table_selection_rejects_nonstring_names(table):
    descriptor, tables = records_dataset()
    with pytest.raises(DatasetContractError, match="table name"):
        Dataset.from_tables(descriptor, tables).to_arrow(table)


def test_serializers_allow_field_exclusion_without_changing_historical_defaults():
    from tests.test_dataset_contract import fixture_dataset

    for descriptor, tables in (records_dataset(), fixture_dataset()):
        model = Dataset.from_tables(descriptor, tables).descriptor
        assert (
            model.model_dump(exclude_none=True, exclude_defaults=True)["profile"] == model.profile
        )
        assert model.model_dump(include={"profile"}) == {"profile": model.profile}
        assert "row_grain" not in model.model_dump(exclude={"row_grain"})
