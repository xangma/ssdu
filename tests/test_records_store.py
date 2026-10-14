"""Synthetic records conformance, historical migration and portable offline replay."""

import hashlib
import shutil
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ssdu.bundles import export_bundle, import_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient, Resource
from ssdu.local_import import LocalImportSpec, _csv_frame, import_local
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.store import Store, canonical


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **kw: pytest.fail("Unexpected HTTP"))


def specification(**changes):
    return {"dataset_id": "invented-survey", "title": "Invented test records", **changes}


def test_records_typed_csv_identifiers_and_explicit_null_policy(tmp_path):
    source = tmp_path / "invented.csv"
    source.write_text("code,estimate,flag\n001,1.25,[c]\n002,,NA\n003,0,\n")
    store = Store(tmp_path / "store")
    manifest = import_local(
        store,
        source,
        specification(
            primary_key=["code"],
            row_grain="One invented survey response per code",
            columns={
                "code": {"label": "Response code", "definition": "Invented identifier"},
                "estimate": {"type": "float64", "unit": "invented units"},
                "flag": {"missing_values": {"[c]": "Suppressed", "NA": "Not applicable"}},
            },
        ),
    )
    dataset = store.open_dataset(manifest["product_id"])
    table = dataset.to_arrow()
    assert table.to_pydict() == {
        "code": ["001", "002", "003"],
        "estimate": [1.25, None, 0.0],
        "flag": ["[c]", "NA", None],
    }
    assert table.schema.field("estimate").type == pa.float64()
    assert dataset.to_arrow("source").equals(table)
    descriptor = dataset.descriptor
    assert descriptor.primary_key == ["code"]
    assert descriptor.row_grain == "One invented survey response per code"
    fields = {field.name: field for field in descriptor.tables[0].columns}
    assert fields["code"].role == "identifier" and fields["code"].nullable is False
    assert fields["estimate"].unit == "invented units"
    assert fields["flag"].missing_values == {"[c]": "Suppressed", "NA": "Not applicable"}
    assert descriptor.coverage.geography == {"status": "unknown"}
    assert descriptor.quality.records_rows == 3
    assert descriptor.quality.observation_rows == 0
    assert descriptor.source.raw_inputs[0].sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert dataset.to_pandas()["code"].tolist() == ["001", "002", "003"]


def test_records_native_parquet_types_are_preserved(tmp_path):
    source = tmp_path / "invented.parquet"
    native = pa.table(
        {
            "code": pa.array(["001", "002"], type=pa.large_string()),
            "signed": pa.array([-2, None], type=pa.int16()),
            "unsigned": pa.array([2, None], type=pa.uint32()),
            "estimate": pa.array([1.25, None], type=pa.float32()),
            "amount": pa.array([Decimal("1234567890.1234"), None], type=pa.decimal128(20, 4)),
            "instant": pa.array(
                [datetime(2026, 1, 1, tzinfo=UTC), None], type=pa.timestamp("us", "UTC")
            ),
        }
    )
    pq.write_table(native, source)
    store = Store(tmp_path / "store")
    manifest = import_local(store, source, specification(primary_key=["code"]))
    dataset = store.open_dataset(manifest["product_id"])
    assert dataset.to_arrow().schema.remove_metadata() == native.schema.remove_metadata()
    assert dataset.to_arrow().to_pydict() == native.to_pydict()
    assert dataset.to_arrow("source").equals(dataset.to_arrow())
    assert dataset.descriptor.source.licence == "Unspecified local rights"


def test_records_declared_composite_keys_grain_and_validation(tmp_path):
    source = tmp_path / "invented.csv"
    source.write_text("code,wave,estimate\n001,1,2\n001,2,3\n")
    store = Store(tmp_path / "store")
    manifest = import_local(
        store,
        source,
        specification(
            primary_key=["code", "wave"],
            row_grain="One response per code and wave",
            columns={"wave": {"type": "int64", "nullable": False}},
        ),
    )
    dataset = store.open_dataset(manifest["product_id"])
    assert dataset.descriptor.primary_key == ["code", "wave"]
    assert dataset.to_arrow().column("wave").to_pylist() == [1, 2]
    assert dataset.descriptor.row_grain == "One response per code and wave"
    before = {item["product_id"] for item in store.list()}
    with pytest.raises(RetrievalError, match="[Kk]ey|unique|duplicate"):
        import_local(store, source, specification(primary_key=["code"]))
    assert {item["product_id"] for item in store.list()} == before
    assert not list(store.snapshots.glob(".staging-*"))


@pytest.mark.parametrize(
    ("text", "declaration"),
    [
        ("code,value\n,1\n", {"primary_key": ["code"]}),
        ("code,value\n001,\n", {"columns": {"value": {"nullable": False}}}),
        ("code,value\n001,1\n", {"primary_key": ["missing"]}),
    ],
)
def test_records_invalid_declarations_publish_nothing(tmp_path, text, declaration):
    source = tmp_path / "invented.csv"
    source.write_text(text)
    store = Store(tmp_path / "store")
    with pytest.raises(RetrievalError):
        import_local(store, source, specification(**declaration))
    assert not list(store.snapshots.iterdir())
    assert not (store.root / "latest.json").exists()
    assert not (store.root / "registry.sqlite").exists()


@pytest.mark.parametrize(
    "declaration",
    [
        {"primary_key": ["code", "code"]},
        {"primary_key": ["code"], "columns": {"code": {"nullable": True}}},
        {"primary_key": ["code"], "columns": {"code": {"role": "attribute"}}},
        {"row_grain": " "},
        {"column_types": {"value": "int64"}, "columns": {"value": {"type": "float64"}}},
    ],
)
def test_conflicting_records_specification_is_rejected(declaration):
    with pytest.raises(ValueError):
        LocalImportSpec.model_validate(specification(**declaration))


def historical_local(store, tmp_path):
    body = b"code,count,note\n001,2,[c]\n002,,NA\n"
    digest = hashlib.sha256(body).hexdigest()
    old_spec = specification(
        column_types={"count": "int64"},
        columns={
            "code": {"label": "Invented code", "definition": "Legacy identifier", "unit": None}
        },
        null_values=[""],
        max_rows=100_000,
        description="",
        licence="Synthetic private rights",
        citation="Invented fixture",
    )
    request = Request(
        "local",
        "invented-survey",
        params={
            "source_sha256": digest,
            "format": "csv",
            "original_filename": "legacy.csv",
            "import_spec": old_spec,
        },
        max_rows=100_000,
    )
    url = f"urn:ssdu:local:sha256:{digest}"
    acquisition = Acquisition(
        "local",
        "invented-survey",
        old_spec["title"],
        _csv_frame(body, LocalImportSpec.model_validate(old_spec)),
        old_spec,
        {"source_sha256": digest, "format": "csv"},
        old_spec["licence"],
        old_spec["citation"],
        url,
    )
    resource = Resource(body, url, "LOCAL_IMPORT", None, "text/csv", {})
    manifest = store._publish(
        request,
        hashlib.sha256(canonical(request.to_dict())).hexdigest(),
        acquisition,
        [resource],
        kind="local",
        transformation_version="local-import-v1",
    )
    assert "dataset_contract" not in manifest
    return manifest


def test_historical_records_migrate_offline_without_rewriting_evidence_or_pointers(tmp_path):
    store = Store(tmp_path / "store")
    parent = historical_local(store, tmp_path)
    original = {
        name: (store.path(parent["product_id"]) / name).read_bytes()
        for name in (*parent["files"], "manifest.json")
    }
    index = (store.root / "latest.json").read_bytes()
    lock = Collections(store).resolve(
        {
            "name": "historical-records",
            "entries": {"legacy": {"snapshot_id": parent["product_id"]}},
        },
        offline=True,
    )
    migrated = store.migrate_dataset(parent["product_id"])
    assert migrated.snapshot_id != parent["product_id"]
    assert migrated.descriptor.profile == "records"
    assert migrated.to_arrow().to_pydict() == {
        "code": ["001", "002"],
        "count": [2, None],
        "note": ["[c]", "NA"],
    }
    assert migrated.descriptor.source.parent_snapshot_ids == [parent["product_id"]]
    assert migrated.descriptor.source.licence == "Synthetic private rights"
    assert migrated.descriptor.source.raw_inputs == []
    assert (store.root / "latest.json").read_bytes() == index
    assert Collections(store).get("historical-records") == lock
    assert store.migrate_dataset(parent["product_id"]).snapshot_id == migrated.snapshot_id
    assert store.open_dataset(migrated.snapshot_id).to_arrow().equals(migrated.to_arrow())
    assert all(
        (store.path(parent["product_id"]) / name).read_bytes() == body
        for name, body in original.items()
    )


def test_historical_records_cache_tracks_all_local_implementation_dependencies(
    tmp_path, monkeypatch
):
    import ssdu.store as store_module
    from ssdu.records import implementation_paths

    store = Store(tmp_path / "store")
    parent = historical_local(store, tmp_path)
    first = store.migrate_dataset(parent["product_id"])
    checksum = store_module.checksum
    altered = next(path for path in implementation_paths() if path.name == "local_import.py")
    monkeypatch.setattr(
        store_module, "checksum", lambda path: "b" * 64 if path == altered else checksum(path)
    )
    second = store.migrate_dataset(parent["product_id"])
    assert first.snapshot_id != second.snapshot_id
    assert first.to_arrow().equals(second.to_arrow())
    assert store.migrate_dataset(parent["product_id"]).snapshot_id == second.snapshot_id
    implementation = store.verify(second.snapshot_id)["implementation_sha256"]
    assert {path.name for path in implementation_paths()} <= set(implementation)
    assert implementation["local_import.py"] == "b" * 64


def test_records_bundle_replays_in_fresh_store_with_original_source_absent(tmp_path):
    store = Store(tmp_path / "source")
    parent = historical_local(store, tmp_path)
    dataset = store.migrate_dataset(parent["product_id"])
    lock = Collections(store).resolve(
        {"name": "portable-records", "entries": {"records": {"snapshot_id": dataset.snapshot_id}}},
        offline=True,
    )
    bundle = tmp_path / "records.zip"
    exported = export_bundle(store, lock, bundle)
    assert set(exported["snapshot_ids"]) == {parent["product_id"], dataset.snapshot_id}
    expected_replay = Collections(store).replay(lock)
    shutil.rmtree(store.root)
    destination = Store(tmp_path / "fresh")
    import_bundle(destination, bundle)
    assert destination.open_dataset(dataset.snapshot_id).to_arrow().equals(dataset.to_arrow())
    assert Collections(destination).replay("portable-records") == expected_replay
    assert destination.migrate_dataset(parent["product_id"]).snapshot_id == dataset.snapshot_id
    assert not (destination.root / "latest.json").exists()


def test_store_import_dataset_returns_common_dataset_api(tmp_path):
    source = tmp_path / "invented.csv"
    source.write_text("code,value\n001,2\n")
    store = Store(tmp_path / "store")
    dataset = store.import_dataset(source, specification(primary_key=["code"]))
    assert dataset.descriptor.profile == "records"
    assert dataset.to_arrow().to_pydict() == {"code": ["001"], "value": ["2"]}
    assert store.open_dataset(dataset.snapshot_id).to_arrow().equals(dataset.to_arrow())
    manifest = store.verify(dataset.snapshot_id)
    cached = store.fetch_dataset(Request(**manifest["request"]), offline=True)
    assert cached.snapshot_id == dataset.snapshot_id
    assert cached.to_arrow().equals(dataset.to_arrow())
