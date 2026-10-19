"""Synthetic deep graph transfer verifies raw evidence and offline cache recovery."""

import copy
import hashlib
import shutil
import stat
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ssdu.connector_cache import CachedClient
from ssdu.http import Resource
from ssdu.metadata_contract import MetadataError, canonical
from ssdu.metadata_graph import entity_id, make_entity, make_link
from ssdu.metadata_graph_store import MetadataGraphStore


def source_resource(**kwargs):
    return Resource(
        canonical({"variables": [{"id": "v1", "label": "Invented count"}], "a/b": {"~c": [0]}}),
        "https://synthetic.example/catalogue?limit=1",
        "GET",
        None,
        "application/json",
        {"etag": '"invented"'},
        captured_at="2026-10-19T09:00:00Z",
        checked_at="2026-10-19T10:00:00Z",
        **kwargs,
    )


def refs(resource, pointer=""):
    return [
        {
            "url": resource.url,
            "body_sha256": hashlib.sha256(resource.body).hexdigest(),
            "pointer": pointer,
            "captured_at": resource.captured_at,
            "checked_at": resource.checked_at,
        }
    ]


def items(resource, number=5):
    return [
        make_entity(
            "invented",
            "origin",
            "variable",
            str(index),
            label=f"Invented variable {index}",
            provenance=refs(resource, "/variables/0"),
        )
        for index in range(number)
    ]


@pytest.fixture
def published(tmp_path):
    store = MetadataGraphStore(tmp_path / "original")
    resource = source_resource()
    entities = items(resource)
    links = [
        make_link(entities[0]["id"], entity["id"], "related_variable", provenance=refs(resource))
        for entity in entities[1:]
    ]
    snapshot_id = store.publish(
        entities, links, [resource], {"status": "complete", "scope": "synthetic only"}
    )
    return store, snapshot_id, entities, links, resource


def test_sharded_pagination_getter_and_order_independence(published, monkeypatch):
    store, snapshot_id, entities, links, resource = published
    from ssdu import metadata_graph_store

    monkeypatch.setattr(metadata_graph_store, "MAX_CHUNK_ROWS", 2)
    sharded = store.publish(
        reversed(entities),
        reversed(links),
        [resource],
        {"status": "complete", "scope": "synthetic only"},
    )
    manifest = store.verify(sharded)
    assert len([name for name in manifest["files"] if name.startswith("entities/")]) == 3
    assert manifest["counts"] == {
        "entities": 5,
        "links": 4,
        "resources": 1,
        "by_kind": {"variable": 5},
    }
    first = store.entities(sharded, limit=2)
    second = store.entities(sharded, limit=2, offset=2)
    third = store.entities(sharded, limit=2, offset=4)
    assert first["next_offset"] == 2 and second["next_offset"] == 4 and third["next_offset"] is None
    assert [row["id"] for row in first["items"] + second["items"] + third["items"]] == sorted(
        item["id"] for item in entities
    )
    assert store.entities(sharded, kind="question")["items"] == []
    assert store.entities(sharded, offset=220_000)["items"] == []
    assert store.links(sharded, limit=2)["next_offset"] == 2
    assert store.links(sharded, offset=220_000)["items"] == []
    assert store.get_entity(sharded, entities[2]["id"]) == entities[2]
    with pytest.raises(MetadataError, match="not present"):
        store.get_entity(sharded, "f" * 64)
    # Same inputs and same shard policy produce one canonical immutable identity.
    assert (
        store.publish(
            entities + [entities[0]],
            links + [links[0]],
            [resource, resource],
            {"status": "complete", "scope": "synthetic only"},
        )
        == sharded
    )
    assert snapshot_id != sharded


def test_deterministic_transfer_restores_exact_scoped_request_and_capture_clocks(
    published, tmp_path, monkeypatch
):
    store, snapshot_id, entities, links, resource = published
    scoped = replace(
        resource,
        method="POST",
        url="https://synthetic.example/detail",
        request_body={"id": "001"},
        cache_scope="origin:release-2",
    )
    more = make_entity("invented", "origin", "question", "q1", provenance=refs(scoped))
    snapshot_id = store.publish(
        entities + [more], links, [resource, scoped], {"status": "complete"}
    )
    manifest = store.verify(snapshot_id)
    bundle = store.export(snapshot_id, tmp_path / "transfer.zip")
    shutil.rmtree(store.root)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: pytest.fail("Network attempted"))
    fresh = MetadataGraphStore(tmp_path / "fresh")
    assert fresh.import_bundle(bundle) == snapshot_id
    assert fresh.import_bundle(bundle) == snapshot_id
    assert fresh.verify(snapshot_id) == manifest
    assert fresh.entities(snapshot_id, limit=100)["items"] == sorted(
        entities + [more], key=lambda item: item["id"]
    )
    assert fresh.export(snapshot_id, tmp_path / "again.zip").read_bytes() == bundle.read_bytes()
    assert fresh.seed_cache(snapshot_id) == {"seeded": 2, "existing": 0}
    assert fresh.seed_all_cache() == {"seeded": 0, "existing": 2, "snapshots": 1}
    with CachedClient(fresh.root, offline=True, allowed_hosts={"synthetic.example"}) as cache:
        replay = cache.get("https://synthetic.example/catalogue", params={"limit": 1})
        assert replay.body == resource.body and replay.cache_hit
        assert (replay.captured_at, replay.checked_at) == (
            resource.captured_at,
            resource.checked_at,
        )
        with cache.scope("origin:release-2"):
            detail = cache.post(scoped.url, json={"id": "001"})
        assert detail.body == scoped.body and detail.cache_scope == scoped.cache_scope
        assert (detail.captured_at, detail.checked_at) == (scoped.captured_at, scoped.checked_at)


def test_cycles_and_unresolved_links_are_retained_and_reported(published):
    store, _, entities, _, resource = published
    a, b = entities[:2]
    missing = entity_id("invented", "other-origin", "variable", "missing")
    links = [
        make_link(a["id"], b["id"], "related_to", provenance=refs(resource)),
        make_link(b["id"], a["id"], "related_to", provenance=refs(resource)),
        make_link(a["id"], missing, "has_question", provenance=refs(resource)),
    ]
    snapshot_id = store.publish(entities, links, [resource], {"status": "partial"})
    report = store.report(snapshot_id)
    assert report["status"] == "partial"
    assert report["graph_validation"]["dangling_links"]["count"] == 1
    assert report["graph_validation"]["cycles"]["present"] is True
    assert report["graph_validation"]["cycles"]["residual_nodes"] == 2
    assert store.links(snapshot_id)["total"] == 3


def test_empty_failed_attempt_can_be_retained_without_claiming_coverage(tmp_path):
    store = MetadataGraphStore(tmp_path)
    snapshot_id = store.publish([], [], [], {"status": "failed", "coverage": None})
    assert store.verify(snapshot_id)["counts"] == {
        "entities": 0,
        "links": 0,
        "resources": 0,
        "by_kind": {},
    }
    assert store.report(snapshot_id)["coverage"] is None


def test_unknown_clocks_survive_transfer_and_reseeding(tmp_path):
    resource = replace(source_resource(), captured_at=None, checked_at=None)
    store = MetadataGraphStore(tmp_path / "original")
    snapshot_id = store.publish(items(resource, 1), [], [resource], {})
    fresh = MetadataGraphStore(tmp_path / "fresh")
    fresh.import_bundle(store.export(snapshot_id, tmp_path / "unknown.zip"))
    fresh.seed_cache(snapshot_id)
    with CachedClient(fresh.root, offline=True, allowed_hosts={"synthetic.example"}) as cache:
        replay = cache.get(resource.url)
    assert replay.captured_at is None and replay.checked_at is None


def test_multiple_snapshots_recover_newest_verified_response_without_losing_original_clock(
    tmp_path,
):
    store = MetadataGraphStore(tmp_path)
    older = source_resource()
    newer = replace(older, body=b'{"updated":"invented"}', checked_at="2026-10-19T12:00:00Z")
    first = store.publish(
        [make_entity("invented", "origin", "dataset", "d1", provenance=refs(older))],
        [],
        [older],
        {},
    )
    second = store.publish(
        [make_entity("invented", "origin", "dataset", "d1", provenance=refs(newer))],
        [],
        [newer],
        {},
    )
    store.seed_cache(first)
    assert store.seed_cache(second) == {"seeded": 1, "existing": 0}
    assert store.seed_cache(first) == {"seeded": 0, "existing": 1}
    shutil.rmtree(store.root / "connector-cache")
    store.seed_all_cache()
    with CachedClient(store.root, offline=True, allowed_hosts={"synthetic.example"}) as cache:
        replay = cache.get(older.url)
    assert replay.body == newer.body
    assert replay.captured_at == older.captured_at
    assert replay.checked_at == newer.checked_at


def test_legitimate_highly_compressible_evidence_roundtrips_under_expansion_guard(tmp_path):
    store = MetadataGraphStore(tmp_path / "original")
    resource = replace(source_resource(), body=b"0" * (2 * 1024 * 1024), content_type="text/plain")
    entity = make_entity("synthetic", "origin", "study", "s1", provenance=refs(resource))
    snapshot_id = store.publish([entity], [], [resource], {})
    bundle = store.export(snapshot_id, tmp_path / "compressible.zip")
    with zipfile.ZipFile(bundle) as archive:
        assert (
            next(
                info for info in archive.infolist() if info.filename.startswith("raw/")
            ).compress_type
            == zipfile.ZIP_STORED
        )
    fresh = MetadataGraphStore(tmp_path / "fresh")
    assert fresh.import_bundle(bundle) == snapshot_id


def test_aggregate_budget_is_enforced_while_staging_not_after_all_rows(tmp_path, monkeypatch):
    from ssdu import metadata_graph_store

    resource = source_resource()
    supplied = []

    def many_entities():
        for entity in items(resource, 20):
            supplied.append(entity)
            yield entity

    monkeypatch.setattr(metadata_graph_store, "MAX_TOTAL_BYTES", 2_000)
    store = MetadataGraphStore(tmp_path)
    with pytest.raises(MetadataError, match="aggregate"):
        store.publish(many_entities(), [], [resource], {})
    assert len(supplied) < 20
    assert store.list()["items"] == [] and not list(store.snapshots.glob(".pending-*"))


@pytest.mark.parametrize(
    "mutation", ["hash", "url", "clock", "pointer", "array-index", "escaped-pointer"]
)
def test_source_evidence_must_resolve_exact_retained_response(tmp_path, mutation):
    resource = source_resource()
    entity = items(resource, 1)[0]
    evidence = entity["provenance"][0]
    if mutation == "hash":
        evidence["body_sha256"] = "0" * 64
    elif mutation == "url":
        evidence["url"] = "https://synthetic.example/unretained"
    elif mutation == "clock":
        evidence["checked_at"] = "2026-10-20T10:00:00Z"
    elif mutation == "pointer":
        evidence["pointer"] = "/absent"
    elif mutation == "array-index":
        evidence["pointer"] = "/variables/00"
    else:
        evidence["pointer"] = "/a~1b/~0c/0"
    store = MetadataGraphStore(tmp_path)
    if mutation == "escaped-pointer":
        assert store.get_entity(store.publish([entity], [], [resource], {}), entity["id"]) == entity
    else:
        with pytest.raises(MetadataError, match="evidence"):
            store.publish([entity], [], [resource], {})
        assert store.list()["items"] == [] and not list(store.snapshots.glob(".pending-*"))


def test_conflicting_id_fails_while_identical_rows_deduplicate(published):
    store, _, entities, links, resource = published
    changed = copy.deepcopy(entities[0])
    changed["label"] = "Conflicting source description"
    with pytest.raises(MetadataError, match="Conflicting"):
        store.publish([*entities, changed], links, [resource], {})


@pytest.mark.parametrize(
    "target",
    [
        "manifest.json",
        "report.json",
        "entities/0000000.jsonl",
        "links/0000000.jsonl",
        "resources/0000000.jsonl",
        "raw",
    ],
)
def test_corruption_blocks_read_export_reseed_without_installing_partial_snapshots(
    published, tmp_path, target
):
    store, snapshot_id, _, _, _ = published
    path = store.path(snapshot_id)
    member = next((path / "raw").iterdir()) if target == "raw" else path / target
    raw = member.read_bytes()
    member.write_bytes(b"x" + raw[1:])
    for action in (
        lambda: store.verify(snapshot_id),
        lambda: store.entities(snapshot_id),
        lambda: store.export(snapshot_id, tmp_path / "bad.zip"),
        lambda: store.seed_cache(snapshot_id),
    ):
        with pytest.raises(MetadataError):
            action()
    assert not (store.root / "connector-cache").exists()


def _rewrite(source, output, *, extra=(), omit=(), mutation=None):
    with zipfile.ZipFile(source) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for info, raw in members:
            if info.filename not in omit:
                archive.writestr(info, mutation(info.filename, raw) if mutation else raw)
        for info, raw in extra:
            archive.writestr(info, raw)
    return output


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "raw/../escape",
        "raw\\escape",
        "raw//escape",
        "raw/./escape",
        "raw/C:escape",
        "manifest.json",
        "extra",
    ],
)
def test_archive_traversal_duplicates_and_unexpected_members_fail_closed(published, tmp_path, name):
    store, snapshot_id, _, _, _ = published
    bundle = store.export(snapshot_id, tmp_path / "source.zip")
    bad = _rewrite(bundle, tmp_path / "bad.zip", extra=[(name, b"invented")])
    fresh = MetadataGraphStore(tmp_path / "fresh")
    with pytest.raises(MetadataError):
        fresh.import_bundle(bad)
    assert fresh.list()["items"] == [] and not list(fresh.snapshots.glob(".pending-*"))
    assert not (tmp_path / "escape").exists()


def test_archive_symlinks_missing_members_and_tampering_fail_closed(published, tmp_path):
    store, snapshot_id, _, _, _ = published
    bundle = store.export(snapshot_id, tmp_path / "source.zip")
    member = "entities/0000000.jsonl"
    info = zipfile.ZipInfo(member)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    for index, kwargs in enumerate(
        (
            {"omit": [member]},
            {"omit": [member], "extra": [(info, b"/etc/passwd")]},
            {"mutation": lambda name, raw: b"x" + raw[1:] if name == member else raw},
        )
    ):
        bad = _rewrite(bundle, tmp_path / f"bad-{index}.zip", **kwargs)
        fresh = MetadataGraphStore(tmp_path / f"fresh-{index}")
        with pytest.raises(MetadataError):
            fresh.import_bundle(bad)
        assert fresh.list()["items"] == [] and not list(fresh.snapshots.glob(".pending-*"))


def test_archive_decompression_budget_is_checked_before_extracting(published, tmp_path):
    store, snapshot_id, _, _, _ = published
    bundle = store.export(snapshot_id, tmp_path / "source.zip")
    huge = zipfile.ZipInfo("raw/" + "0" * 64 + ".bin")
    huge.compress_type = zipfile.ZIP_DEFLATED
    bad = _rewrite(bundle, tmp_path / "bad.zip", extra=[(huge, b"0" * (2 * 1024 * 1024))])
    fresh = MetadataGraphStore(tmp_path / "fresh")
    with pytest.raises(MetadataError, match="Unsafe"):
        fresh.import_bundle(bad)
    assert not list(fresh.snapshots.glob(".pending-*"))


def test_store_rejects_symlink_namespace_snapshot_and_extra_files(published, tmp_path):
    store, snapshot_id, _, _, _ = published
    path = store.path(snapshot_id)
    (path / "extra").write_text("invented")
    with pytest.raises(MetadataError, match="extra"):
        store.verify(snapshot_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    other = tmp_path / "unsafe"
    other.mkdir()
    (other / "metadata").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MetadataError, match="symlinks"):
        MetadataGraphStore(other)


def test_concurrent_publication_is_atomic_and_content_addressed(published):
    store, snapshot_id, entities, links, resource = published
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda _: store.publish(
                    entities, links, [resource], {"status": "complete", "scope": "synthetic only"}
                ),
                range(6),
            )
        )
    assert results == [snapshot_id] * 6
    assert store.list()["total"] == 1
    assert not list(store.snapshots.glob(".pending-*"))


@pytest.mark.parametrize(
    "limit,offset", [(True, 0), (0, 0), (101, 0), (1, -1), (1, True), (1, 8_000_001)]
)
def test_pagination_limits_reject_unbounded_requests(published, limit, offset):
    store, snapshot_id, _, _, _ = published
    with pytest.raises(MetadataError):
        store.entities(snapshot_id, limit=limit, offset=offset)
