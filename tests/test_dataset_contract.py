"""Contract conformance is semantic, independent of any provider or live network."""

from copy import deepcopy
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ssdu.dataset_contract import (
    CONTRACT_VERSION,
    MAX_DESCRIPTOR_BYTES,
    Dataset,
    DatasetContractError,
)


def fixture_dataset():
    table = pa.table(
        {
            "area": pa.array(["001", "002"], type=pa.string()),
            "measure": pa.array(["income", "income"], type=pa.string()),
            "value": pa.array([100.5, None], type=pa.float64()),
            "value_raw": pa.array(["100.50", ":"], type=pa.string()),
            "unit": pa.array(["GBP", "GBP"], type=pa.string()),
            "status": pa.array([None, "suppressed"], type=pa.string()),
            "notes": pa.array([None, "Provider withheld this value"], type=pa.string()),
        }
    )
    roles = {name: name for name in table.column_names}
    roles["area"] = "dimension"
    descriptor = {
        "contract_version": CONTRACT_VERSION,
        "profile": "observations",
        "dataset_id": "example:income",
        "title": "Household income",
        "source": {
            "provider": "example",
            "dataset_id": "income",
            "url": "https://example.org/income",
            "licence": "https://example.org/licence",
            "citation": "Income table, release 2025",
            "release": {"year": 2025},
            "effective_request": {"provider": "example", "dataset_id": "income"},
            "raw_inputs": [{"path": "raw/0.csv", "sha256": "a" * 64, "bytes": 45}],
            "parent_snapshot_ids": [],
        },
        "transform": {"id": "example-observations", "version": "1.0.0"},
        "dimensions": [
            {
                "id": "area",
                "column": "area",
                "label": "Area identifier",
                "kind": "identifier",
                "codes": [{"code": "001", "label": "A"}, {"code": "002", "label": "B"}],
            }
        ],
        "variables": [{"id": "income", "label": "Income", "unit": "GBP"}],
        "tables": [
            {
                "name": "observations",
                "path": "tables/observations.parquet",
                "rows": 2,
                "columns": [
                    {
                        "name": field.name,
                        "dtype": "float64" if pa.types.is_float64(field.type) else str(field.type),
                        "role": roles[field.name],
                        "nullable": field.name not in ("measure", "area"),
                    }
                    for field in table.schema
                ],
            }
        ],
        "quality": {"source_rows": 2, "observation_rows": 2, "completeness": "complete"},
        "coverage": {"geography": {"status": "unknown"}, "temporal": {"status": "unknown"}},
    }
    return descriptor, {"observations": table}


def test_roundtrip_retains_semantics_and_provenance(tmp_path):
    descriptor, tables = fixture_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    files = dataset.write(tmp_path)
    reopened = Dataset.read(tmp_path, snapshot_id="b" * 64)
    assert reopened.snapshot_id == "b" * 64
    assert reopened.table_names == ("observations",)
    assert reopened.contract == dataset.contract
    assert reopened.to_arrow().equals(dataset.to_arrow())
    assert reopened.to_pandas()["area"].tolist() == ["001", "002"]
    assert reopened.to_arrow()["value_raw"].to_pylist() == ["100.50", ":"]
    assert reopened.to_arrow()["status"].to_pylist() == [None, "suppressed"]
    assert reopened.descriptor.coverage.temporal == {"status": "unknown"}
    assert reopened.descriptor.source.release == {"year": 2025}
    assert set(files) == {"dataset.json", "tables/observations.parquet"}
    assert all(len(value["sha256"]) == 64 and value["bytes"] > 0 for value in files.values())
    assert "b" * 64 not in (tmp_path / "dataset.json").read_text()


def test_descriptor_copies_and_input_dict_are_isolated():
    descriptor, tables = fixture_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    descriptor["title"] = "Changed"
    copy = dataset.descriptor
    copy.source.release["year"] = 2026
    mapping = dataset.contract
    mapping["dimensions"][0]["codes"].clear()
    assert dataset.descriptor.title == "Household income"
    assert dataset.descriptor.source.release["year"] == 2025
    assert len(dataset.descriptor.dimensions[0].codes) == 2


@pytest.mark.parametrize("version", ["2.0.0", "0.1.0", "1.1.0", "1", 1, None])
def test_unsupported_version_fails_closed(version):
    descriptor, tables = fixture_dataset()
    descriptor["contract_version"] = version
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_observations_rejected(value):
    descriptor, tables = fixture_dataset()
    table = tables["observations"]
    tables["observations"] = table.set_column(
        table.column_names.index("value"), "value", pa.array([value, None], type=pa.float64())
    )
    with pytest.raises(DatasetContractError, match="finite"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("nullable", [False, True])
def test_null_dimension_requires_nullable_declaration_and_unknown_diagnostic(nullable):
    descriptor, tables = fixture_dataset()
    descriptor["dimensions"][0]["nullable"] = nullable
    descriptor["tables"][0]["columns"][0]["nullable"] = nullable
    tables["observations"] = tables["observations"].set_column(
        0, "area", pa.array(["001", None], type=pa.string())
    )
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)
    descriptor["quality"]["diagnostics"] = [
        {"code": "unknown_dimension", "message": "Source has no code", "column": "area", "count": 1}
    ]
    if nullable:
        assert Dataset.from_tables(descriptor, tables).to_arrow()["area"].null_count == 1
    else:
        with pytest.raises(DatasetContractError):
            Dataset.from_tables(descriptor, tables)


def test_unknown_diagnostic_count_cannot_understate_nulls():
    descriptor, tables = fixture_dataset()
    descriptor["dimensions"][0]["nullable"] = True
    descriptor["tables"][0]["columns"][0]["nullable"] = True
    descriptor["quality"]["diagnostics"] = [
        {"code": "unknown_dimension", "message": "Unknown", "column": "area", "count": 0}
    ]
    tables["observations"] = tables["observations"].set_column(
        0, "area", pa.array(["001", None], type=pa.string())
    )
    with pytest.raises(DatasetContractError, match="unknown diagnostics"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize(
    "column, values", [("area", ["001", "999"]), ("measure", ["income", "other"])]
)
def test_undeclared_codes_and_measures_rejected(column, values):
    descriptor, tables = fixture_dataset()
    table = tables["observations"]
    tables["observations"] = table.set_column(
        table.column_names.index(column), column, pa.array(values, type=pa.string())
    )
    with pytest.raises(DatasetContractError, match="absent"):
        Dataset.from_tables(descriptor, tables)


def test_nullable_measure_identifier_rejected():
    descriptor, tables = fixture_dataset()
    descriptor["tables"][0]["columns"][1]["nullable"] = True
    with pytest.raises(DatasetContractError, match="Measure identifier"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("scope", ["dimensions", "variables", "tables", "codes", "columns", "raw"])
def test_duplicate_identifiers_rejected(scope):
    descriptor, tables = fixture_dataset()
    if scope == "codes":
        target = descriptor["dimensions"][0]["codes"]
    elif scope == "columns":
        target = descriptor["tables"][0]["columns"]
    elif scope == "raw":
        target = descriptor["source"]["raw_inputs"]
    else:
        target = descriptor[scope]
    target.append(deepcopy(target[0]))
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("scope", ["quality", "schema", "arrow", "source"])
def test_row_count_mismatches_rejected(scope):
    descriptor, tables = fixture_dataset()
    if scope == "quality":
        descriptor["quality"]["observation_rows"] = 3
    elif scope == "schema":
        descriptor["tables"][0]["rows"] = 3
    elif scope == "arrow":
        tables["observations"] = tables["observations"].slice(0, 1)
    else:
        descriptor["tables"].append(
            {
                "name": "source",
                "path": "tables/source.parquet",
                "rows": 1,
                "columns": [{"name": "original", "dtype": "string", "role": "attribute"}],
            }
        )
        tables["source"] = pa.table({"original": ["one"]})
    with pytest.raises(DatasetContractError, match="row count"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize("mutation", ["type", "role", "extra", "order", "nullable"])
def test_declared_arrow_schema_is_enforced(mutation):
    descriptor, tables = fixture_dataset()
    table = tables["observations"]
    if mutation == "type":
        tables["observations"] = table.set_column(
            2, "value", pa.array([100, None], type=pa.int64())
        )
    elif mutation == "role":
        descriptor["tables"][0]["columns"][0]["role"] = "attribute"
    elif mutation == "extra":
        tables["observations"] = table.append_column("extra", pa.array(["x", "y"]))
    elif mutation == "order":
        tables["observations"] = table.select(list(reversed(table.column_names)))
    else:
        tables["observations"] = table.set_column(1, "measure", pa.array(["income", None]))
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


def test_original_source_fields_and_attribute_labels_roundtrip(tmp_path):
    descriptor, tables = fixture_dataset()
    original = pa.table({"Original field name": pa.array(["100.50", ":"]), "Weight": [1.0, 2.0]})
    tables["source"] = original
    descriptor["tables"].append(
        {
            "name": "source",
            "path": "tables/source.parquet",
            "rows": 2,
            "columns": [
                {"name": "Original field name", "dtype": "string", "role": "attribute"},
                {"name": "Weight", "dtype": "float64", "role": "attribute"},
            ],
        }
    )
    tables["observations"] = tables["observations"].append_column(
        "Area label", pa.array(["A", "B"])
    )
    descriptor["tables"][0]["columns"].append(
        {"name": "Area label", "dtype": "string", "role": "attribute"}
    )
    dataset = Dataset.from_tables(descriptor, tables)
    dataset.write(tmp_path)
    reopened = Dataset.read(tmp_path)
    assert reopened.to_arrow("source").equals(original)
    assert reopened.to_arrow()["Area label"].to_pylist() == ["A", "B"]


@pytest.mark.parametrize(
    "path",
    [
        "../outside.parquet",
        "/outside.parquet",
        "tables/wrong.parquet",
        "tables\\observations.parquet",
    ],
)
def test_unsafe_or_noncanonical_table_paths_rejected(path):
    descriptor, tables = fixture_dataset()
    descriptor["tables"][0]["path"] = path
    with pytest.raises(DatasetContractError, match="canonical contract path"):
        Dataset.from_tables(descriptor, tables)


@pytest.mark.parametrize(
    "path", ["../outside.csv", "/outside.csv", "raw/../outside.csv", "raw\\0.csv", "raw//0.csv"]
)
def test_unsafe_raw_paths_rejected(path):
    descriptor, tables = fixture_dataset()
    descriptor["source"]["raw_inputs"][0]["path"] = path
    with pytest.raises(DatasetContractError, match="safe relative path"):
        Dataset.from_tables(descriptor, tables)


def test_writes_do_not_overwrite_existing_dataset(tmp_path):
    descriptor, tables = fixture_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    dataset.write(tmp_path)
    before = (tmp_path / "dataset.json").read_bytes()
    with pytest.raises(DatasetContractError, match="existing"):
        dataset.write(tmp_path)
    assert (tmp_path / "dataset.json").read_bytes() == before


def test_symlink_tables_not_written_or_read(tmp_path):
    descriptor, tables = fixture_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    other = tmp_path / "other"
    other.mkdir()
    root = tmp_path / "dataset"
    root.mkdir()
    (root / "tables").symlink_to(other, target_is_directory=True)
    with pytest.raises(DatasetContractError, match="symlinks"):
        dataset.write(root)
    (root / "tables").unlink()
    dataset.write(root)
    parquet = root / "tables/observations.parquet"
    retained = other / "observations.parquet"
    parquet.rename(retained)
    parquet.symlink_to(retained)
    with pytest.raises(DatasetContractError, match="symlinks"):
        Dataset.read(root)


def test_read_validates_semantics_of_modified_parquet(tmp_path):
    descriptor, tables = fixture_dataset()
    dataset = Dataset.from_tables(descriptor, tables)
    dataset.write(tmp_path)
    wrong = tables["observations"].set_column(0, "area", pa.array(["001", "undeclared"]))
    pq.write_table(wrong, tmp_path / "tables/observations.parquet")
    with pytest.raises(DatasetContractError, match="absent"):
        Dataset.read(tmp_path)


@pytest.mark.parametrize(
    "scope, value",
    [("unknown", "x"), ("source_rows", True), ("release", {"value": datetime(2025, 1, 1)})],
)
def test_strict_metadata_and_json_types(scope, value):
    descriptor, tables = fixture_dataset()
    if scope == "source_rows":
        descriptor["quality"][scope] = value
    elif scope == "release":
        descriptor["source"][scope] = value
    else:
        descriptor[scope] = value
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


def test_descriptor_size_and_nesting_are_bounded():
    descriptor, tables = fixture_dataset()
    descriptor["source"]["release"] = {"large": "x" * MAX_DESCRIPTOR_BYTES}
    with pytest.raises(DatasetContractError, match="8 MiB"):
        Dataset.from_tables(descriptor, tables)
    descriptor, tables = fixture_dataset()
    nested = {}
    for _ in range(40):
        nested = {"nested": nested}
    descriptor["source"]["release"] = nested
    with pytest.raises(DatasetContractError, match="nesting"):
        Dataset.from_tables(descriptor, tables)


def test_missing_dataset_and_invalid_snapshot_binding(tmp_path):
    with pytest.raises(DatasetContractError, match="Cannot read"):
        Dataset.read(tmp_path)
    descriptor, tables = fixture_dataset()
    with pytest.raises(DatasetContractError, match="full SHA"):
        Dataset.from_tables(descriptor, tables, snapshot_id="abc")
    with pytest.raises(DatasetContractError, match="Unknown dataset table"):
        Dataset.from_tables(descriptor, tables).to_arrow("missing")


def test_inspect_checks_schema_without_reading_rows(tmp_path, monkeypatch):
    descriptor, tables = fixture_dataset()
    Dataset.from_tables(descriptor, tables).write(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("inspect must use Parquet schema metadata")

    monkeypatch.setattr(pq, "read_table", forbidden)
    inspected = Dataset.inspect(tmp_path)
    assert inspected.quality.observation_rows == 2
    assert inspected.contract_version == "1.0.0"


@pytest.mark.parametrize("mutation", ["rows", "type"])
def test_inspect_rejects_changed_parquet_schema(tmp_path, mutation):
    descriptor, tables = fixture_dataset()
    Dataset.from_tables(descriptor, tables).write(tmp_path)
    wrong = tables["observations"]
    if mutation == "rows":
        wrong = wrong.slice(0, 1)
    else:
        wrong = wrong.set_column(2, "value", pa.array([100, None], type=pa.int64()))
    pq.write_table(wrong, tmp_path / "tables/observations.parquet")
    with pytest.raises(DatasetContractError):
        Dataset.inspect(tmp_path)


@pytest.mark.parametrize("reader", [Dataset.read, Dataset.inspect])
def test_readers_reject_duplicate_json_keys(tmp_path, reader):
    descriptor, tables = fixture_dataset()
    Dataset.from_tables(descriptor, tables).write(tmp_path)
    path = tmp_path / "dataset.json"
    original = path.read_text()
    path.write_text('{"contract_version":"2.0.0",' + original[1:])
    with pytest.raises(DatasetContractError, match="duplicate JSON"):
        reader(tmp_path)


@pytest.mark.parametrize("field", ["contract_version", "profile"])
def test_version_and_profile_must_be_explicit(field):
    descriptor, tables = fixture_dataset()
    del descriptor[field]
    with pytest.raises(DatasetContractError):
        Dataset.from_tables(descriptor, tables)


def test_source_bound_dataset_identity_and_snapshot_parents():
    descriptor, tables = fixture_dataset()
    descriptor["dataset_id"] = "another:income"
    with pytest.raises(DatasetContractError, match="identity differs"):
        Dataset.from_tables(descriptor, tables)
    descriptor["dataset_id"] = "example:income"
    descriptor["source"]["parent_snapshot_ids"] = ["b" * 64]
    dataset = Dataset.from_tables(descriptor, tables, snapshot_id="c" * 64)
    assert dataset.descriptor.source.parent_snapshot_ids == ["b" * 64]
    assert "c" * 64 not in str(dataset.contract)
    descriptor["source"]["parent_snapshot_ids"] *= 2
    with pytest.raises(DatasetContractError, match="bindings must be unique"):
        Dataset.from_tables(descriptor, tables)


def test_nested_source_json_is_copied_from_input():
    descriptor, tables = fixture_dataset()
    descriptor["source"]["release"] = {"values": [2025]}
    dataset = Dataset.from_tables(descriptor, tables)
    descriptor["source"]["release"]["values"].append(2026)
    assert dataset.descriptor.source.release == {"values": [2025]}


def test_direct_constructor_cannot_bypass_validation():
    descriptor, tables = fixture_dataset()
    descriptor["quality"]["observation_rows"] = 99
    with pytest.raises(DatasetContractError, match="row count"):
        Dataset(descriptor, tables)


@pytest.mark.parametrize("binding", [123, True, "ABC", "f" * 63])
def test_snapshot_binding_is_strict(binding):
    descriptor, tables = fixture_dataset()
    with pytest.raises(DatasetContractError, match="full SHA"):
        Dataset.from_tables(descriptor, tables, snapshot_id=binding)
