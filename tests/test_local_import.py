"""Offline imports preserve identifiers, typing decisions and immutable evidence."""

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from ssdu import local_import
from ssdu.http import HttpClient
from ssdu.local_import import MAX_BYTES, LocalImportSpec, import_local
from ssdu.models import RetrievalError
from ssdu.registry import Registry
from ssdu.store import Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))


@pytest.fixture
def csv_source(tmp_path):
    source = tmp_path / "private-input" / "survey.csv"
    source.parent.mkdir()
    source.write_bytes(
        b"code,label,count,rate,approved,notes\n00123,NA,2,1.25,TRUE,\n00007,Available,,2.5,0,NA\n"
    )
    return source


def spec(**options):
    return {"dataset_id": "local-survey", "title": "Local survey", **options}


def test_default_csv_strings_exact_bytes_private_path_and_offline_registration(
    tmp_path, csv_source
):
    store = Store(tmp_path / "store")
    original = csv_source.read_bytes()
    manifest = import_local(
        store,
        csv_source,
        spec(
            description="Researcher supplied observations",
            columns={
                "code": {"label": "Area", "definition": "Original area identifier", "unit": "code"}
            },
        ),
    )
    frame = store.replay(manifest["product_id"])
    assert frame["code"].tolist() == ["00123", "00007"]
    assert frame["label"].tolist() == ["NA", "Available"]
    assert frame["count"].iloc[0] == "2"
    assert pd.isna(frame["count"].iloc[1])
    assert pd.isna(frame["notes"].iloc[0])
    assert frame["notes"].iloc[1] == "NA"
    assert all(str(dtype).startswith("string") for dtype in frame.dtypes)
    snapshot = store.path(manifest["product_id"])
    assert (snapshot / "raw/0000.bin").read_bytes() == original
    digest = hashlib.sha256(original).hexdigest()
    assert manifest["source_url"] == f"urn:ssdu:local:sha256:{digest}"
    assert manifest["resources"][0]["method"] == "LOCAL_IMPORT"
    assert manifest["files"]["raw/0000.bin"]["sha256"] == digest
    assert manifest["kind"] == "local"
    assert manifest["transformation_version"] == "local-import-v1"
    metadata = json.loads((snapshot / "metadata.json").read_bytes())
    assert metadata["original_filename"] == "survey.csv"
    assert metadata["columns"]["code"]["definition"] == "Original area identifier"
    assert metadata["description"] == "Researcher supplied observations"
    assert str(csv_source.parent) not in json.dumps({"manifest": manifest, "metadata": metadata})
    registry = Registry(store.root).get("local:local-survey")
    assert registry["current_snapshot_id"] == manifest["product_id"]
    assert registry["availability"]["queryable"] is True
    assert store.verify(manifest["product_id"]) == manifest


def test_explicit_csv_types_and_nullable_values(tmp_path, csv_source):
    store = Store(tmp_path / "store")
    manifest = import_local(
        store,
        csv_source,
        LocalImportSpec(
            **spec(
                column_types={"count": "int64", "rate": "float64", "approved": "boolean"},
            )
        ),
    )
    frame = store.replay(manifest["product_id"])
    assert frame["code"].tolist() == ["00123", "00007"]
    assert frame["count"].iloc[0] == 2 and pd.isna(frame["count"].iloc[1])
    assert frame["rate"].tolist() == [1.25, 2.5]
    assert frame["approved"].tolist() == [True, False]
    schema = pq.read_schema(store.path(manifest["product_id"]) / "data.parquet")
    assert schema.field("count").type == pa.int64()
    assert schema.field("rate").type == pa.float64()
    assert schema.field("approved").type == pa.bool_()


def test_null_tokens_are_explicit_and_exact(tmp_path, csv_source):
    store = Store(tmp_path / "store")
    manifest = import_local(store, csv_source, spec(null_values=["NA"]))
    frame = store.replay(manifest["product_id"])
    assert pd.isna(frame["label"].iloc[0])
    assert pd.isna(frame["notes"].iloc[1])
    assert frame["notes"].iloc[0] == ""  # Empty is not a declared null in this import.
    assert frame["count"].iloc[1] == ""


def test_identical_import_is_idempotent_and_source_is_read_once(tmp_path, csv_source, monkeypatch):
    store = Store(tmp_path / "store")
    old = import_local(store, csv_source, spec())
    open_original = Path.open
    calls = []

    class SingleRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            calls.append(size)
            assert len(calls) == 1
            return self.stream.read(size)

    def open_once(path, *args, **kwargs):
        stream = open_original(path, *args, **kwargs)
        return SingleRead(stream) if path == csv_source else stream

    monkeypatch.setattr(Path, "open", open_once)
    again = import_local(store, csv_source, spec())
    assert old == again
    assert calls == [MAX_BYTES + 1]
    assert len(store.list()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Revised title"},
        {"description": "Revised interpretation"},
        {"licence": "Research consent restrictions"},
        {"citation": "Survey team (2026)"},
        {"columns": {"code": {"definition": "Revised code definition"}}},
        {"column_types": {"code": "string"}},
        {"null_values": ["", "NA"]},
    ],
)
def test_metadata_changes_create_versions_without_overwriting(tmp_path, csv_source, change):
    store = Store(tmp_path / "store")
    old = import_local(store, csv_source, spec())
    old_path = store.path(old["product_id"])
    old_files = {name: (old_path / name).read_bytes() for name in old["files"]}
    new = import_local(store, csv_source, spec(**change))
    assert old["product_id"] != new["product_id"]
    assert old["request"] != new["request"]
    assert all((old_path / name).read_bytes() == body for name, body in old_files.items())
    assert store.verify(old["product_id"]) == old
    assert (
        Registry(store.root).get("local:local-survey")["current_snapshot_id"] == new["product_id"]
    )


def test_changed_file_versions_keep_previous_raw_and_replay(tmp_path, csv_source):
    store = Store(tmp_path / "store")
    old = import_local(store, csv_source, spec())
    previous = csv_source.read_bytes()
    csv_source.write_bytes(previous.replace(b"00123", b"00999"))
    new = import_local(store, csv_source, spec())
    assert old["product_id"] != new["product_id"]
    assert store.replay(old["product_id"])["code"].iloc[0] == "00123"
    assert store.replay(new["product_id"])["code"].iloc[0] == "00999"
    assert (store.path(old["product_id"]) / "raw/0000.bin").read_bytes() == previous


@pytest.mark.parametrize(
    ("dtype", "value"),
    [
        ("int64", "1.5"),
        ("int64", "9223372036854775808"),
        ("float64", "NaN"),
        ("float64", "1e999"),
        ("boolean", "yes"),
    ],
)
def test_invalid_explicit_values_publish_nothing(tmp_path, dtype, value):
    source = tmp_path / "bad.csv"
    source.write_text(f"value\n{value}\n")
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="invalid"):
        import_local(store, source, spec(column_types={"value": dtype}))
    assert store.list() == []


@pytest.mark.parametrize(
    "options",
    [
        {"column_types": {"absent": "int64"}},
        {"columns": {"absent": {"label": "Missing"}}},
    ],
)
def test_unknown_columns_are_rejected(tmp_path, csv_source, options):
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="unknown columns"):
        import_local(store, csv_source, spec(**options))
    assert store.list() == []


@pytest.mark.parametrize(
    "options",
    [
        {"dataset_id": "../escape"},
        {"title": " "},
        {"max_rows": 100001},
        {"max_rows": True},
        {"unknown": True},
        {"column_types": {"code": "int32"}},
        {"columns": {"code": {"invented": "value"}}},
        {"null_values": ("",)},
    ],
)
def test_spec_is_strict(options):
    with pytest.raises(ValidationError):
        LocalImportSpec.model_validate(spec(**options))


def test_csv_row_limit_is_rejection_not_truncation(tmp_path, csv_source):
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="max_rows=1"):
        import_local(store, csv_source, spec(max_rows=1))
    assert store.list() == []


@pytest.mark.parametrize(
    "body",
    [
        b"a,a\n1,2\n",
        b"a,b\n1,2,3\n",
        b"a,b\n1\n",
        b"a,b\n",
        b"a\n\xff\n",
    ],
)
def test_malformed_or_empty_csv_rejected_without_silent_field_loss(tmp_path, body):
    source = tmp_path / "bad.csv"
    source.write_bytes(body)
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError):
        import_local(store, source, spec())
    assert store.list() == []


def test_byte_limit_unsupported_extension_and_non_file(tmp_path):
    store = Store(tmp_path / "store")
    source = tmp_path / "large.csv"
    with source.open("wb") as stream:
        stream.truncate(MAX_BYTES + 1)
    with pytest.raises(RetrievalError, match="25 MB"):
        import_local(store, source, spec())
    with pytest.raises(RetrievalError, match="supports"):
        import_local(store, tmp_path / "unknown.xlsx", spec())
    with pytest.raises(RetrievalError, match="regular file"):
        import_local(store, tmp_path / "missing.csv", spec())
    assert store.list() == []


@pytest.fixture
def parquet_source(tmp_path):
    source = tmp_path / "typed.parquet"
    table = pa.table(
        {
            "code": pa.array(["001", "NA", ""], type=pa.string()),
            "count": pa.array([1, None, 3], type=pa.int32()),
            "approved": pa.array([True, False, None], type=pa.bool_()),
            "day": pa.array([date(2026, 1, 1), None, date(2026, 1, 3)], type=pa.date32()),
            "instant": pa.array(
                [datetime(2026, 1, 1), None, datetime(2026, 1, 3)], type=pa.timestamp("us")
            ),
        }
    )
    pq.write_table(table, source)
    return source, table


def test_parquet_preserves_native_codes_nulls_widths_and_temporal_types(tmp_path, parquet_source):
    source, original = parquet_source
    store = Store(tmp_path / "store")
    manifest = import_local(store, source, spec())
    retained = pq.read_table(store.path(manifest["product_id"]) / "data.parquet")
    assert retained.schema.remove_metadata() == original.schema.remove_metadata()
    assert retained.to_pydict() == original.to_pydict()
    assert retained.column("code").to_pylist() == ["001", "NA", ""]
    assert (store.path(manifest["product_id"]) / "raw/0000.bin").read_bytes() == source.read_bytes()
    assert import_local(store, source, spec()) == manifest


def test_parquet_compatible_explicit_cast(tmp_path, parquet_source):
    source, _ = parquet_source
    store = Store(tmp_path / "store")
    manifest = import_local(store, source, spec(column_types={"count": "int64"}))
    table = pq.read_table(store.path(manifest["product_id"]) / "data.parquet")
    assert table.schema.field("count").type == pa.int64()
    assert table.column("count").to_pylist() == [1, None, 3]


@pytest.mark.parametrize(("value", "dtype"), [(1.5, "int64"), (2**53 + 1, "float64")])
def test_parquet_lossy_cast_rejected(tmp_path, value, dtype):
    source = tmp_path / "lossy.parquet"
    pq.write_table(pa.table({"value": [value]}), source)
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="losslessly"):
        import_local(store, source, spec(column_types={"value": dtype}))
    assert store.list() == []


def test_parquet_incompatible_cast_and_row_bound(tmp_path, parquet_source, monkeypatch):
    source, _ = parquet_source
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="incompatible"):
        import_local(store, source, spec(column_types={"code": "int64"}))
    monkeypatch.setattr(pq.ParquetFile, "read", lambda *a, **k: pytest.fail("Oversized decode"))
    with pytest.raises(RetrievalError, match="max_rows=2"):
        import_local(store, source, spec(max_rows=2))
    assert store.list() == []


def test_invalid_parquet_rejected(tmp_path):
    source = tmp_path / "bad.parquet"
    source.write_bytes(b"not a parquet file")
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="valid Parquet"):
        import_local(store, source, spec())
    assert store.list() == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_parquet_non_finite_floats_are_rejected_without_converting_to_null(tmp_path, value):
    source = tmp_path / "nonfinite.parquet"
    pq.write_table(pa.table({"value": pa.array([None, 1.0, value])}), source)
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="non-finite floats"):
        import_local(store, source, spec())
    assert store.list() == []


@pytest.mark.parametrize("values", [[b"\xff"], [[1, 2]], [{"nested": "value"}]])
def test_parquet_unsupported_values_fail_before_decode(tmp_path, values, monkeypatch):
    source = tmp_path / "unsupported.parquet"
    pq.write_table(pa.table({"value": values}), source)
    monkeypatch.setattr(pq.ParquetFile, "read", lambda *a, **k: pytest.fail("Unsupported decode"))
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="unsupported type"):
        import_local(store, source, spec())
    assert store.list() == []


def test_parquet_footer_budget_rejects_compressed_payload_before_decode(tmp_path, monkeypatch):
    source = tmp_path / "compressed.parquet"
    pq.write_table(
        pa.table({"text": ["x" * 2000] * 100}), source, compression="gzip", use_dictionary=False
    )
    assert source.stat().st_size < 100_000
    monkeypatch.setattr(local_import, "MAX_DECODED_BYTES", 100_000)
    monkeypatch.setattr(pq.ParquetFile, "read", lambda *a, **k: pytest.fail("Over-budget decode"))
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="footer"):
        import_local(store, source, spec())
    assert store.list() == []


def test_parquet_dictionary_expansion_checked_before_pandas(tmp_path, monkeypatch):
    source = tmp_path / "dictionary.parquet"
    pq.write_table(pa.table({"text": ["x" * 7000] * 20}), source, use_dictionary=True)
    monkeypatch.setattr(local_import, "MAX_DECODED_BYTES", 100_000)
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="Decoded Parquet"):
        import_local(store, source, spec())
    assert store.list() == []


@pytest.mark.parametrize("format_name", ["csv", "parquet"])
def test_column_limit_is_rejected_before_publication(tmp_path, format_name):
    source = tmp_path / f"wide.{format_name}"
    columns = [f"column_{number}" for number in range(1001)]
    if format_name == "csv":
        source.write_text(",".join(columns) + "\n" + ",".join(["value"] * 1001) + "\n")
    else:
        pq.write_table(pa.table({name: [1] for name in columns}), source)
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError, match="1000 column limit"):
        import_local(store, source, spec())
    assert store.list() == []
