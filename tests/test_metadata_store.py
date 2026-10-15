"""Synthetic metadata snapshots verify closure, immutable identity and offline portability."""

import copy
import hashlib
import json
import stat
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_metadata_contract import synthetic_document

from ssdu import metadata_store
from ssdu.metadata_contract import MetadataError, canonical
from ssdu.metadata_store import MetadataStore


@pytest.fixture
def published(tmp_path):
    store = MetadataStore(tmp_path / "original")
    document = synthetic_document()
    resources = {"raw/catalogue.json": b'{"invented":"metadata only"}\n'}
    snapshot_id = store.publish(document, resources)
    return store, snapshot_id, document, resources


def _rewrite(source, output, *, mutate=None, omit=(), extra=()):
    with zipfile.ZipFile(source) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for info, raw in members:
            if info.filename not in omit:
                archive.writestr(info, mutate(info.filename, raw) if mutate else raw)
        for info, raw in extra:
            archive.writestr(info, raw)
    return output


def test_snapshot_roundtrip_and_offline_replay_keep_same_identity_and_every_reference(
    published, tmp_path, monkeypatch
):
    store, snapshot_id, document, resources = published
    manifest = store.verify(snapshot_id)
    assert manifest["counts"] == {"providers": 1, "records": 2}
    assert manifest["contract_version"] == "1.0.0"
    assert manifest["files"]["resources/raw/catalogue.json"] == {
        "sha256": hashlib.sha256(resources["raw/catalogue.json"]).hexdigest(),
        "bytes": len(resources["raw/catalogue.json"]),
    }
    bundle = store.export(snapshot_id, tmp_path / "capture.zip")
    assert bundle == tmp_path / "capture.zip"
    store.root.rename(tmp_path / "unavailable-original")
    monkeypatch.setattr("socket.socket", lambda *a, **k: pytest.fail("Network access attempted"))
    fresh = MetadataStore(tmp_path / "fresh")
    assert fresh.import_bundle(bundle) == snapshot_id
    assert fresh.verify(snapshot_id) == manifest
    assert fresh.read(snapshot_id) == document
    assert fresh.import_bundle(bundle) == snapshot_id
    second = fresh.export(snapshot_id, tmp_path / "second.zip")
    assert bundle.read_bytes() == second.read_bytes()
    assert fresh.list() == {
        "rows": [
            {
                "snapshot_id": snapshot_id,
                "counts": {"providers": 1, "records": 2},
                "captured_at": document["captured_at"],
            }
        ],
        "total": 1,
        "limit": 20,
        "offset": 0,
        "next_offset": None,
    }


def test_publication_is_canonical_concurrent_and_append_only(published, tmp_path):
    store, original_id, document, resources = published
    reversed_document = copy.deepcopy(document)
    reversed_document["records"].reverse()
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: store.publish(reversed_document, resources), range(8)))
    assert ids == [original_id] * 8
    changed = copy.deepcopy(document)
    changed["records"][0]["title"] = "Changed invented label"
    later = store.publish(changed, resources)
    assert later != original_id
    assert store.read(original_id) == document
    assert store.read(later)["records"][0]["title"] == "Changed invented label"
    first = store.list(limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1
    assert store.list(limit=1, offset=1)["next_offset"] is None
    assert store.list(offset=2)["rows"] == []
    assert not list(store.snapshots.glob(".pending-*"))


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "raw/../escape",
        "raw\\file",
        "raw//file",
        "raw/./file",
        "raw/C:escape",
        "raw/\x00file",
    ],
)
def test_publish_rejects_unsafe_resource_names(published, name):
    store, _, document, resources = published
    resources[name] = b"invented"
    with pytest.raises(MetadataError, match="Unsafe"):
        store.publish(document, resources)


def test_publish_rejects_missing_evidence_and_path_conflicts(published):
    store, _, document, resources = published
    with pytest.raises(MetadataError, match="Missing raw"):
        store.publish(document, {})
    resources["raw"] = b"conflict"
    with pytest.raises(MetadataError, match="conflict"):
        store.publish(document, resources)


@pytest.mark.parametrize(
    "target", ["document.json", "resources/raw/catalogue.json", "manifest.json"]
)
def test_tampered_snapshot_is_never_read_exported_or_overwritten(published, target, tmp_path):
    store, snapshot_id, document, resources = published
    path = store.path(snapshot_id) / target
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"Invented", b"Changed") if b"Invented" in raw else b"x" + raw[1:])
    for operation in (
        lambda: store.verify(snapshot_id),
        lambda: store.read(snapshot_id),
        lambda: store.export(snapshot_id, tmp_path / "bad.zip"),
        lambda: store.publish(document, resources),
    ):
        with pytest.raises(MetadataError):
            operation()
    assert path.read_bytes() != raw


def test_snapshot_inventory_rejects_extra_members_and_symlinks(published, tmp_path):
    store, snapshot_id, _, _ = published
    path = store.path(snapshot_id)
    (path / "extra").write_bytes(b"invented")
    with pytest.raises(MetadataError, match="extra members"):
        store.read(snapshot_id)
    (path / "extra").unlink()
    body = path / "resources/raw/catalogue.json"
    outside = tmp_path / "outside"
    outside.write_bytes(body.read_bytes())
    body.unlink()
    body.symlink_to(outside)
    with pytest.raises(MetadataError, match="unsafe file"):
        store.read(snapshot_id)


def test_store_rejects_symlink_namespaces_and_snapshot_directories(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "store"
    root.mkdir()
    (root / "metadata").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MetadataError, match="symlinks"):
        MetadataStore(root)
    (root / "metadata").unlink()
    store = MetadataStore(root)
    (store.snapshots / ("a" * 64)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(MetadataError, match="missing or unsafe"):
        store.path("a" * 64)


@pytest.mark.parametrize("member", ["../escape", "resources/extra", "manifest.json"])
def test_import_rejects_traversal_extra_or_duplicate_members(published, tmp_path, member):
    store, snapshot_id, _, _ = published
    bundle = store.export(snapshot_id, tmp_path / "source.zip")
    corrupt = _rewrite(bundle, tmp_path / "corrupt.zip", extra=[(member, b"invented")])
    fresh = MetadataStore(tmp_path / "fresh")
    with pytest.raises(MetadataError):
        fresh.import_bundle(corrupt)
    assert fresh.list()["rows"] == []
    assert not list(fresh.snapshots.glob(".pending-*"))
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "mutation", ["missing", "tampered", "symlink", "duplicate_json", "schema", "noncanonical"]
)
def test_import_rejects_missing_tampered_unsafe_and_forged_manifests(published, tmp_path, mutation):
    store, snapshot_id, _, _ = published
    bundle = store.export(snapshot_id, tmp_path / "source.zip")
    extra, omit = [], []
    if mutation == "missing":
        omit = ["resources/raw/catalogue.json"]
    if mutation == "symlink":
        omit = ["resources/raw/catalogue.json"]
        info = zipfile.ZipInfo("resources/raw/catalogue.json")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        extra = [(info, b"/etc/passwd")]

    def mutate(name, raw):
        if mutation == "tampered" and name.startswith("resources/"):
            return b"x" * len(raw)
        if mutation == "duplicate_json" and name == "manifest.json":
            return b'{"schema_version":1,' + raw[1:]
        if mutation == "schema" and name == "manifest.json":
            value = json.loads(raw)
            value["schema_version"] = 9
            return canonical(value)
        if mutation == "noncanonical":
            # Rebuild the inventory hash consistently; validation must still reject
            # the re-ordered document rather than treating it as an accepted identity.
            if name == "document.json":
                value = json.loads(raw)
                value["records"].reverse()
                return canonical(value) + b"\n"
            if name == "manifest.json":
                value = json.loads(raw)
                document = json.loads(
                    store.path(snapshot_id).joinpath("document.json").read_bytes()
                )
                document["records"].reverse()
                body = canonical(document) + b"\n"
                value["files"]["document.json"] = {
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                }
                value.pop("snapshot_id")
                value["snapshot_id"] = hashlib.sha256(canonical(value)).hexdigest()
                return canonical(value)
        return raw

    corrupt = _rewrite(bundle, tmp_path / "corrupt.zip", mutate=mutate, omit=omit, extra=extra)
    fresh = MetadataStore(tmp_path / "fresh")
    with pytest.raises(MetadataError):
        fresh.import_bundle(corrupt)
    assert fresh.list()["rows"] == []
    assert not list(fresh.snapshots.glob(".pending-*"))


def test_size_limits_are_enforced_before_snapshot_publication(published, monkeypatch):
    store, _, document, resources = published
    monkeypatch.setattr(metadata_store, "MAX_RESOURCE_BYTES", 2)
    with pytest.raises(MetadataError, match="bounded bytes"):
        store.publish(document, resources)
    monkeypatch.setattr(metadata_store, "MAX_RESOURCE_BYTES", 1024)
    monkeypatch.setattr(metadata_store, "MAX_TOTAL_BYTES", 20)
    with pytest.raises(MetadataError, match="total size"):
        store.publish(document, resources)


def test_export_refuses_overwrite_and_snapshot_destinations(published, tmp_path):
    store, snapshot_id, _, _ = published
    output = store.export(snapshot_id, tmp_path / "bundle.zip")
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        store.export(snapshot_id, output)
    assert output.read_bytes() == original
    with pytest.raises(MetadataError, match="immutable"):
        store.export(snapshot_id, store.path(snapshot_id) / "bundle.zip")


@pytest.mark.parametrize(
    "limit,offset", [(True, 0), (0, 0), (101, 0), (20, -1), (20, True), (20, 100001)]
)
def test_pagination_rejects_unbounded_or_boolean_arguments(published, limit, offset):
    with pytest.raises(MetadataError):
        published[0].list(limit=limit, offset=offset)
