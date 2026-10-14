"""Portable transfer preserves pinned results, existing captures and collection visibility."""

import hashlib
import json
import shutil
import stat
import warnings
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pandas as pd
import pyarrow as pa
import pytest
from pyarrow import parquet

from ssdu import bundles
from ssdu.adapters import deprivation
from ssdu.bundles import export_bundle, import_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.local_import import import_local
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.store import Store, canonical


@pytest.fixture
def collection(tmp_path, iod_csv):
    store = Store(tmp_path / "source")
    with HttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=iod_csv))
    ) as client:
        selected = store.fetch(
            Request(
                "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
            ),
            client=client,
        )
    lock = Collections(store).resolve(
        {
            "name": "area-scores",
            "description": "Pinned deprivation scores with source flags",
            "entries": {
                "scores": {
                    "snapshot_id": selected["product_id"],
                    "query": {"columns": ["lsoa_code", "imd_score", "imd_score_flag"]},
                }
            },
        },
        offline=True,
    )
    return store, lock


def archive_members(path):
    with zipfile.ZipFile(path) as archive:
        return [(info, archive.read(info)) for info in archive.infolist()]


def rewrite(source, target, *, transform=None, extra=(), omit=()):
    members = archive_members(source)
    with warnings.catch_warnings(), zipfile.ZipFile(target, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for info, raw in members:
            if info.filename in omit:
                continue
            if transform:
                raw = transform(info.filename, raw)
            archive.writestr(info, raw)
        for info, raw in extra:
            archive.writestr(info, raw)
    return target


def exported(collection, tmp_path):
    store, lock = collection
    output = tmp_path / "collection.zip"
    report = export_bundle(store, lock, output)
    return output, report


def assert_unpublished(store):
    assert not list(store.snapshots.iterdir())
    assert Collections(store).list() == []
    assert not list(store.root.glob(".bundle-import-*"))


def test_export_complete_parent_closure_and_offline_fresh_transfer(
    collection, tmp_path, monkeypatch
):
    source, lock = collection
    archive_path, exported_report = exported(collection, tmp_path)
    selected_id = lock.entries["scores"].snapshot_id
    parent_id = source.verify(selected_id)["parent_snapshot_ids"][0]
    assert exported_report["snapshot_ids"] == sorted([selected_id, parent_id])
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert json.loads(archive.read("collection.lock.json")) == lock.model_dump()
        assert "registry.sqlite" not in names
        assert all(not name.startswith("/") for name in names)
        for snapshot_id in exported_report["snapshot_ids"]:
            manifest = source.verify(snapshot_id)
            assert {name for name in names if name.startswith(f"snapshots/{snapshot_id}/")} == {
                f"snapshots/{snapshot_id}/manifest.json",
                *(f"snapshots/{snapshot_id}/{name}" for name in manifest["files"]),
            }
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    destination = Store(tmp_path / "destination")
    imported = import_bundle(destination, archive_path)
    assert imported["current"] is True
    assert imported["imported_snapshot_count"] == 2
    assert imported["bundle_id"] == exported_report["bundle_id"]
    assert Collections(destination).replay("area-scores") == Collections(source).replay(lock)
    assert not (destination.root / "latest.json").exists()
    assert not (destination.root / "sources.json").exists()
    catalogue = destination.root / "collections" / "area-scores" / f"catalog-{lock.lock_id}.yaml"
    assert str(destination.snapshots) in catalogue.read_text()
    assert str(source.snapshots) not in catalogue.read_text()


def test_import_is_idempotent_and_export_does_not_overwrite(collection, tmp_path):
    archive_path, report = exported(collection, tmp_path)
    original_archive = archive_path.read_bytes()
    source, lock = collection
    with pytest.raises(FileExistsError):
        export_bundle(source, lock, archive_path)
    assert archive_path.read_bytes() == original_archive
    destination = Store(tmp_path / "destination")
    import_bundle(destination, archive_path)
    manifests = {
        s: (destination.path(s) / "manifest.json").read_bytes() for s in report["snapshot_ids"]
    }
    again = import_bundle(destination, archive_path)
    assert again["imported_snapshot_count"] == 0
    assert again["current"] is True
    assert manifests == {s: (destination.path(s) / "manifest.json").read_bytes() for s in manifests}


def test_export_rejects_existing_dangling_symlink(collection, tmp_path):
    source, lock = collection
    target = tmp_path / "not-created.zip"
    output = tmp_path / "existing-link.zip"
    output.symlink_to(target)
    with pytest.raises(FileExistsError):
        export_bundle(source, lock, output)
    assert output.is_symlink()
    assert not target.exists()


def test_existing_different_current_is_preserved_until_activation(collection, tmp_path):
    source, first = collection
    first_path, _ = exported(collection, tmp_path)
    second = Collections(source).resolve(
        {
            "name": "area-scores",
            "entries": {
                "scores": {
                    "snapshot_id": first.entries["scores"].snapshot_id,
                    "query": {"count_only": True},
                }
            },
        },
        offline=True,
    )
    second_path = tmp_path / "second.zip"
    export_bundle(source, second, second_path)
    destination = Store(tmp_path / "destination")
    import_bundle(destination, first_path)
    preserved = import_bundle(destination, second_path)
    assert preserved["current"] is False
    assert Collections(destination).get("area-scores").lock_id == first.lock_id
    assert Collections(destination).get(second.lock_id).lock_id == second.lock_id
    activated = import_bundle(destination, second_path, activate=True)
    assert activated["current"] is True
    assert activated["imported_snapshot_count"] == 0
    assert Collections(destination).get("area-scores").lock_id == second.lock_id


def test_tampered_data_rejected_before_any_snapshot_publication(collection, tmp_path):
    archive_path, _ = exported(collection, tmp_path)
    altered = rewrite(
        archive_path,
        tmp_path / "altered.zip",
        transform=lambda name, raw: raw + b"tampered" if name.endswith("data.parquet") else raw,
    )
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="size|hash"):
        import_bundle(destination, altered)
    assert_unpublished(destination)


def declared_archive(contents, path):
    """Recompute the outer envelope, leaving inner snapshot/lock checks decisive."""
    document = json.loads(contents["bundle.json"])
    document["lock_id"] = json.loads(contents["collection.lock.json"])["lock_id"]
    document["snapshot_ids"] = sorted(
        {name.split("/")[1] for name in contents if name.startswith("snapshots/")}
    )
    document["members"] = {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        for name, raw in contents.items()
        if name != "bundle.json"
    }
    document["bundle_id"] = hashlib.sha256(
        canonical({k: v for k, v in document.items() if k != "bundle_id"})
    ).hexdigest()
    contents["bundle.json"] = canonical(document)
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in contents.items():
            archive.writestr(name, raw)
    return path


def test_altered_locked_result_digest_rejected_before_publication(collection, tmp_path):
    archive_path, _ = exported(collection, tmp_path)
    contents = {info.filename: raw for info, raw in archive_members(archive_path)}
    lock = json.loads(contents["collection.lock.json"])
    lock["entries"]["scores"]["result_sha256"] = "0" * 64
    lock["lock_id"] = hashlib.sha256(
        canonical({k: v for k, v in lock.items() if k != "lock_id"})
    ).hexdigest()
    contents["collection.lock.json"] = canonical(lock)
    altered = declared_archive(contents, tmp_path / "altered-result.zip")
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="query result changed"):
        import_bundle(destination, altered)
    assert_unpublished(destination)


@pytest.mark.parametrize("change", ["rows", "dtype"])
def test_physical_parquet_must_match_even_rehashed_manifest(collection, tmp_path, change):
    archive_path, _ = exported(collection, tmp_path)
    contents = {info.filename: raw for info, raw in archive_members(archive_path)}
    lock = json.loads(contents["collection.lock.json"])
    old_id = lock["entries"]["scores"]["snapshot_id"]
    manifest = json.loads(contents[f"snapshots/{old_id}/manifest.json"])
    if change == "rows":
        manifest["rows"] += 1
    else:
        manifest["columns"]["imd_score"] = "string"
    new_id = hashlib.sha256(
        canonical({k: v for k, v in manifest.items() if k != "product_id"})
    ).hexdigest()
    manifest["product_id"] = new_id
    contents = {name.replace(old_id, new_id): raw for name, raw in contents.items()}
    contents[f"snapshots/{new_id}/manifest.json"] = canonical(manifest)
    lock["entries"]["scores"]["snapshot_id"] = new_id
    lock["spec"]["entries"]["scores"]["snapshot_id"] = new_id
    lock["lock_id"] = hashlib.sha256(
        canonical({k: v for k, v in lock.items() if k != "lock_id"})
    ).hexdigest()
    contents["collection.lock.json"] = canonical(lock)
    altered = declared_archive(contents, tmp_path / "altered-physical.zip")
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="physical Parquet"):
        import_bundle(destination, altered)
    assert_unpublished(destination)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "snapshots\\escape", "C:/escape"])
def test_unsafe_member_paths_rejected(collection, tmp_path, name):
    archive_path, _ = exported(collection, tmp_path)
    altered = rewrite(archive_path, tmp_path / "unsafe.zip", extra=[(name, b"bad")])
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="unsafe"):
        import_bundle(destination, altered)
    assert_unpublished(destination)
    assert not (tmp_path / "escape").exists()


def test_duplicate_and_symlink_members_rejected(collection, tmp_path):
    archive_path, _ = exported(collection, tmp_path)
    duplicate = rewrite(
        archive_path, tmp_path / "duplicate.zip", extra=[("collection.lock.json", b"duplicate")]
    )
    symlink_info = zipfile.ZipInfo("link")
    symlink_info.create_system = 3
    symlink_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    symlink = rewrite(archive_path, tmp_path / "symlink.zip", extra=[(symlink_info, b"../escape")])
    for index, path in enumerate([duplicate, symlink]):
        destination = Store(tmp_path / f"destination-{index}")
        with pytest.raises(RetrievalError, match="duplicate or unsupported"):
            import_bundle(destination, path)
        assert_unpublished(destination)


def test_missing_parent_rejected_even_with_updated_bundle_manifest(collection, tmp_path):
    source, lock = collection
    archive_path, _ = exported(collection, tmp_path)
    selected = lock.entries["scores"].snapshot_id
    parent = source.verify(selected)["parent_snapshot_ids"][0]
    omitted = [
        info.filename for info, _ in archive_members(archive_path) if parent in info.filename
    ]

    def transform(name, raw):
        if name != "bundle.json":
            return raw
        document = json.loads(raw)
        document["snapshot_ids"].remove(parent)
        for member in omitted:
            del document["members"][member]
        document["bundle_id"] = hashlib.sha256(
            canonical({k: v for k, v in document.items() if k != "bundle_id"})
        ).hexdigest()
        return canonical(document)

    altered = rewrite(
        archive_path, tmp_path / "missing-parent.zip", transform=transform, omit=omitted
    )
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="parent missing"):
        import_bundle(destination, altered)
    assert_unpublished(destination)


def test_corrupt_existing_destination_capture_is_never_overwritten(collection, tmp_path):
    source, lock = collection
    archive_path, _ = exported(collection, tmp_path)
    parent = source.verify(lock.entries["scores"].snapshot_id)["parent_snapshot_ids"][0]
    destination = Store(tmp_path / "destination")
    shutil.copytree(source.path(parent), destination.snapshots / parent)
    raw_path = destination.path(parent) / "raw/0000.bin"
    raw_path.write_bytes(b"corrupt destination")
    with pytest.raises(RetrievalError, match="hash mismatch"):
        import_bundle(destination, archive_path)
    assert raw_path.read_bytes() == b"corrupt destination"
    assert len(list(destination.snapshots.iterdir())) == 1
    assert Collections(destination).list() == []


def test_interrupted_snapshot_publication_rolls_back_new_directories(
    collection, tmp_path, monkeypatch
):
    archive_path, _ = exported(collection, tmp_path)
    destination = Store(tmp_path / "destination")
    original_rename = bundles.os.rename
    renames = 0

    def interrupted(source, target):
        nonlocal renames
        renames += 1
        if renames == 2:
            raise KeyboardInterrupt("Interrupted second snapshot publication")
        return original_rename(source, target)

    monkeypatch.setattr(bundles.os, "rename", interrupted)
    with pytest.raises(KeyboardInterrupt):
        import_bundle(destination, archive_path)
    assert renames == 2
    assert_unpublished(destination)


def test_interrupt_after_successful_rename_still_rolls_back(collection, tmp_path, monkeypatch):
    archive_path, _ = exported(collection, tmp_path)
    destination = Store(tmp_path / "destination")
    original_rename = bundles.os.rename

    def interrupted(source, target):
        original_rename(source, target)
        raise KeyboardInterrupt("Signal immediately after successful rename")

    monkeypatch.setattr(bundles.os, "rename", interrupted)
    with pytest.raises(KeyboardInterrupt):
        import_bundle(destination, archive_path)
    assert_unpublished(destination)


def test_failed_collection_install_rolls_back_new_snapshots_preserving_existing(
    collection, tmp_path, monkeypatch
):
    source, lock = collection
    archive_path, _ = exported(collection, tmp_path)
    parent = source.verify(lock.entries["scores"].snapshot_id)["parent_snapshot_ids"][0]
    destination = Store(tmp_path / "destination")
    shutil.copytree(source.path(parent), destination.snapshots / parent)
    original_manifest = (destination.path(parent) / "manifest.json").read_bytes()
    original_install = Collections.install_lock

    def broken_install(manager, *args, **kwargs):
        if manager.store.root == destination.root:
            raise RuntimeError("Collection installation interrupted")
        return original_install(manager, *args, **kwargs)

    monkeypatch.setattr(Collections, "install_lock", broken_install)
    with pytest.raises(RetrievalError, match="could not be imported"):
        import_bundle(destination, archive_path)
    assert [path.name for path in destination.snapshots.iterdir()] == [parent]
    assert (destination.path(parent) / "manifest.json").read_bytes() == original_manifest
    assert destination.verify(parent)["rows"] == 4
    assert Collections(destination).list() == []


def test_archive_byte_budget_applies_before_extraction(collection, tmp_path, monkeypatch):
    archive_path, _ = exported(collection, tmp_path)
    destination = Store(tmp_path / "destination")
    monkeypatch.setattr(bundles, "MAX_BYTES", 64)
    with pytest.raises(RetrievalError, match="byte budget"):
        import_bundle(destination, archive_path)
    assert_unpublished(destination)


def test_encrypted_member_flag_rejected_before_reading_archive_payload(collection, tmp_path):
    archive_path, _ = exported(collection, tmp_path)
    raw = bytearray(archive_path.read_bytes())
    local_header = raw.index(b"PK\x03\x04")
    central_header = raw.index(b"PK\x01\x02")
    raw[local_header + 6] |= 1
    raw[central_header + 8] |= 1
    encrypted = tmp_path / "encrypted.zip"
    encrypted.write_bytes(raw)
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="unsupported archive member"):
        import_bundle(destination, encrypted)
    assert_unpublished(destination)


def test_excessively_compressed_member_rejected_before_extraction(collection, tmp_path):
    archive_path, _ = exported(collection, tmp_path)
    info = zipfile.ZipInfo("bomb.bin")
    info.compress_type = zipfile.ZIP_DEFLATED
    bomb = rewrite(archive_path, tmp_path / "bomb.zip", extra=[(info, b"\x00" * (2 * 1024 * 1024))])
    destination = Store(tmp_path / "destination")
    with pytest.raises(RetrievalError, match="compression ratio"):
        import_bundle(destination, bomb)
    assert_unpublished(destination)


@pytest.mark.parametrize("limit", ["MAX_DEPTH", "MAX_SNAPSHOTS"])
def test_export_lineage_bounds_are_checked_before_store_verification(
    collection, tmp_path, monkeypatch, limit
):
    source, lock = collection
    monkeypatch.setattr(bundles, limit, 0 if limit == "MAX_DEPTH" else 1)
    monkeypatch.setattr(source, "verify", lambda *a, **k: pytest.fail("Unbounded store traversal"))
    output = tmp_path / "not-published.zip"
    with pytest.raises(RetrievalError, match="budget"):
        export_bundle(source, lock, output)
    assert not output.exists()


def test_export_detects_file_change_while_writing_and_publishes_nothing(
    collection, tmp_path, iod_csv, monkeypatch
):
    source, lock = collection
    parent = source.verify(lock.entries["scores"].snapshot_id)["parent_snapshot_ids"][0]
    raw_record = source.verify(parent)["files"]["raw/0000.bin"]
    original_copy = bundles._copy_member

    def changed_while_copying(input_stream, output_stream, expected):
        if expected == raw_record:
            (source.path(parent) / "raw/0000.bin").write_bytes(
                iod_csv.replace(b"12.375", b"13.375")
            )
        return original_copy(input_stream, output_stream, expected)

    monkeypatch.setattr(bundles, "_copy_member", changed_while_copying)
    output = tmp_path / "not-published.zip"
    with pytest.raises(RetrievalError, match="hash mismatch"):
        export_bundle(source, lock, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".bundle-*.zip"))


def test_arrow_temporal_and_decimal_parquet_transfers_with_pinned_values(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    table = pa.table(
        {
            "code": pa.array(["001", "002"]),
            "when": pa.array(
                [datetime(2025, 1, 1, tzinfo=UTC), None], type=pa.timestamp("us", tz="UTC")
            ),
            "day": pa.array([date(2025, 1, 1), None], type=pa.date32()),
            "amount": pa.array([Decimal("12.34"), None], type=pa.decimal128(10, 2)),
        }
    )
    source_path = tmp_path / "typed.parquet"
    parquet.write_table(table, source_path)
    source = Store(tmp_path / "source")
    snapshot = import_local(
        source, source_path, {"dataset_id": "typed-survey", "title": "Typed survey"}
    )
    lock = Collections(source).resolve(
        {"name": "typed", "entries": {"values": {"snapshot_id": snapshot["product_id"]}}},
        offline=True,
    )
    output = tmp_path / "typed.zip"
    export_bundle(source, lock, output)
    destination = Store(tmp_path / "destination")
    report = import_bundle(destination, output)
    assert report["imported_snapshot_count"] == 1
    actual = Collections(destination).replay("typed")
    assert actual == Collections(source).replay(lock)
    rows = actual["entries"]["values"]["rows"]
    assert rows[0]["code"] == "001"
    assert rows[0]["when"].startswith("2025-01-01T00:00:00")
    assert rows[0]["day"] == "2025-01-01"
    assert rows[0]["amount"] == "12.34"
    assert rows[1] == {"code": "002", "when": None, "day": None, "amount": None}
    assert (
        parquet.read_schema(
            destination.path(snapshot["product_id"]) / "data.parquet"
        ).remove_metadata()
        == table.schema
    )


def test_ordinary_pandas_timezone_snapshot_transfers_without_arrow_dtype(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    source = Store(tmp_path / "source")
    frame = pd.DataFrame({"when": pd.to_datetime(["2025-01-01T00:00:00Z", None])})
    request = Request("local", "legacy-timezone")
    snapshot = source._publish(
        request,
        hashlib.sha256(canonical(request.to_dict())).hexdigest(),
        Acquisition(
            "local",
            "legacy-timezone",
            "Legacy timezone frame",
            frame,
            {},
            {},
            "Unspecified local rights",
            "Fixture",
            "urn:ssdu:fixture:legacy-timezone",
        ),
        [],
    )
    assert snapshot["columns"]["when"] == "datetime64[ns, UTC]"
    lock = Collections(source).resolve(
        {"name": "legacy", "entries": {"values": {"snapshot_id": snapshot["product_id"]}}},
        offline=True,
    )
    output = tmp_path / "legacy.zip"
    export_bundle(source, lock, output)
    destination = Store(tmp_path / "destination")
    import_bundle(destination, output)
    assert Collections(destination).replay("legacy") == Collections(source).replay(lock)
