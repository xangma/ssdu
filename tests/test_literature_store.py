"""Frozen paper–dataset evidence survives verified transfer and retains unknowns."""

import copy
import hashlib
import json
import stat
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from ssdu import literature_store
from ssdu.literature_store import LiteratureError, LiteratureStore, validate_document


@pytest.fixture
def document():
    source = {
        "id": "rc",
        "title": "Rich Context frozen capture",
        "url": "https://example.org/rc/commit/abc",
        "licence": None,
        "version": "abc",
        "resource_paths": ["rc/publications.json"],
    }
    uk = {
        "id": "uk-paper",
        "title": "Portsmouth deprivation and employment",
        "url": "https://example.org/portsmouth-paper",
        "published_at": "2024-02-01",
        "type": "research_report",
        "resource_paths": ["uk/evidence.json"],
    }
    refs = [{key: value for key, value in source.items() if key in {"id", "title", "url"}}]
    uk_refs = [
        {
            key: value
            for key, value in uk.items()
            if key in {"id", "title", "url", "published_at", "type"}
        }
    ]
    papers = [
        {
            "id": "paper-z",
            "title": "Portsmouth deprivation and employment",
            "doi": None,
            "url": uk["url"],
            "abstract": None,
            "published_at": "2024-02-01",
            "source_ids": ["uk-paper"],
            "references": uk_refs,
            "metadata_provenance": [{"field": "title", "source_id": "uk-paper"}],
        },
        {
            "id": "paper-a",
            "title": "Rural household poverty",
            "doi": "10.1234/poverty",
            "url": "https://example.org/poverty-paper",
            "abstract": "Household poverty and policy outcomes.",
            "published_at": None,
            "source_ids": ["rc"],
            "references": refs,
        },
    ]
    datasets = [
        {
            "id": "nomis",
            "title": "Claimant count",
            "provider": "Nomis",
            "native_id": "NM_1_1",
            "description": "Monthly claimant count",
            "doi": None,
            "version": None,
            "coverage": {"places": ["Portsmouth"]},
            "source_ids": ["uk-paper"],
            "references": uk_refs,
        },
        {
            "id": "survey",
            "title": "Household survey",
            "provider": "Survey programme",
            "native_id": None,
            "description": "",
            "doi": None,
            "version": None,
            "coverage": {},
            "source_ids": ["rc"],
            "references": refs,
            "asset_precision": "programme",
        },
    ]
    relationships = []
    for identifier, paper_id, dataset_id, relationship_type, evidence_source, citations in (
        ("r1", "paper-a", "survey", "source_asserted_use", "rc", refs),
        ("r2", "paper-z", "nomis", "verified_use", "uk-paper", uk_refs),
        ("r3", "paper-z", "survey", "inferred_mention", "uk-paper", uk_refs),
    ):
        relationships.append(
            {
                "id": identifier,
                "paper_id": paper_id,
                "dataset_id": dataset_id,
                "relationship_type": relationship_type,
                "asserted_by": "Recorded source mapping",
                "evidence": {
                    "source_id": evidence_source,
                    "location": f"source row {identifier}",
                    "summary": "Source explicitly identifies the provider; release unknown.",
                    "url": next(
                        item["url"] for item in (source, uk) if item["id"] == evidence_source
                    ),
                },
                "dataset_version": None,
                "review": {
                    "status": "source_reviewed" if identifier == "r1" else "pending",
                    "reviewer": "Rich Context curator team (as reported by repository)"
                    if identifier == "r1"
                    else None,
                    "independent_validation": False,
                },
                "references": citations,
            }
        )
    return {
        "schema_version": 1,
        "sources": [source, uk],
        "papers": papers,
        "datasets": datasets,
        "relationships": relationships,
        "generation": {"method": "bounded_source_import", "human_validated": False},
    }


@pytest.fixture
def resources():
    return {
        "rc/publications.json": b'{"source":"pinned"}\n',
        "uk/evidence.json": b'{"usage":"checked"}\n',
    }


def _rewrite(source, destination, *, mutate=None, extra=(), omit=()):
    with zipfile.ZipFile(source) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    with warnings.catch_warnings(), zipfile.ZipFile(destination, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for info, raw in members:
            if info.filename not in omit:
                archive.writestr(info, mutate(info.filename, raw) if mutate else raw)
        for info, raw in extra:
            archive.writestr(info, raw)
    return destination


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _redeclare_manifest(source, destination, transform):
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    transform(manifest)
    identity = {key: value for key, value in manifest.items() if key != "snapshot_id"}
    manifest["snapshot_id"] = hashlib.sha256(_canonical(identity)).hexdigest()
    members["manifest.json"] = _canonical(manifest) + b"\n"
    with zipfile.ZipFile(destination, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return destination


def _assert_empty(store):
    assert store.list() == []
    assert list(store.snapshots.iterdir()) == []


def test_actual_fresh_store_transfer_preserves_identity_search_raws_and_unknowns(
    tmp_path, document, resources
):
    source = LiteratureStore(tmp_path / "source")
    snapshot_id = source.publish(document, resources)
    manifest = source.verify(snapshot_id)
    expected = validate_document(document, resources)
    assert source.read(snapshot_id) == expected
    assert manifest["counts"] == {"sources": 2, "papers": 2, "datasets": 2, "relationships": 3}
    assert (
        manifest["files"]["resources/rc/publications.json"]["sha256"]
        == hashlib.sha256(resources["rc/publications.json"]).hexdigest()
    )
    baseline = source.search(snapshot_id, "Portsmouth", relationship_type="verified_use")
    assert baseline["total"] == 1
    assert baseline["rows"][0]["paper"]["abstract"] is None
    link = baseline["rows"][0]["relationships"][0]
    assert link["relationship"]["dataset_version"] is None
    assert link["relationship"]["review"]["status"] == "pending"
    archive = tmp_path / "evidence.zip"
    source.export(snapshot_id, archive)
    fresh = LiteratureStore(tmp_path / "fresh")
    assert fresh.import_bundle(archive) == snapshot_id
    assert fresh.verify(snapshot_id) == manifest
    assert fresh.read(snapshot_id) == expected
    assert fresh.search(snapshot_id, "Portsmouth", relationship_type="verified_use") == baseline
    for name in manifest["files"]:
        assert (fresh.path(snapshot_id) / name).read_bytes() == (
            source.path(snapshot_id) / name
        ).read_bytes()
    assert not (fresh.root / "snapshots").exists()
    assert not (fresh.directory / "registry.sqlite").exists()


def test_every_search_row_and_nested_link_has_readable_references(tmp_path, document, resources):
    store = LiteratureStore(tmp_path)
    snapshot_id = store.publish(document, resources)
    result = store.search(snapshot_id)
    for row in result["rows"]:
        assert row["references"] == row["paper"]["references"]
        assert all(ref["title"] and ref["url"] for ref in row["references"])
        for link in row["relationships"]:
            assert link["dataset"]["references"]
            assert link["relationship"]["references"]
            assert link["relationship"]["evidence"]["location"]
    assert result["rows"][0]["paper"]["id"] == "paper-a"
    assert (
        result["rows"][0]["relationships"][0]["relationship"]["relationship_type"]
        == "source_asserted_use"
    )


def test_canonical_order_idempotence_and_old_versions_retained(tmp_path, document, resources):
    store = LiteratureStore(tmp_path)
    first = store.publish(document, resources)
    reordered = copy.deepcopy(document)
    for key in ("sources", "papers", "datasets", "relationships"):
        reordered[key].reverse()
    assert store.publish(reordered, dict(reversed(list(resources.items())))) == first
    newer = copy.deepcopy(document)
    newer["papers"][0]["abstract"] = "Newly captured abstract."
    second = store.publish(newer, resources)
    assert second != first
    assert store.read(first)["papers"][1]["abstract"] is None
    assert store.read(second)["papers"][1]["abstract"] == "Newly captured abstract."
    assert len(store.list()) == 2
    assert store.read(first)["generation"] == document["generation"]
    assert store.read(first)["datasets"][1]["asset_precision"] == "programme"


def test_parallel_publication_and_repeated_import_are_idempotent(tmp_path, document, resources):
    store = LiteratureStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _: store.publish(document, resources), range(8)))
    assert len(set(ids)) == 1
    assert len(store.list()) == 1
    assert not list(store.snapshots.glob(".pending-*"))
    archive = tmp_path / "bundle.zip"
    store.export(ids[0], archive)
    assert store.import_bundle(archive) == ids[0]
    assert len(store.list()) == 1


def test_export_is_deterministic_and_never_overwrites(tmp_path, document, resources):
    store = LiteratureStore(tmp_path / "store")
    snapshot_id = store.publish(document, resources)
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    store.export(snapshot_id, first)
    store.export(snapshot_id, second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        store.export(snapshot_id, first)
    link = tmp_path / "dangling.zip"
    link.symlink_to(tmp_path / "missing.zip")
    with pytest.raises(FileExistsError):
        store.export(snapshot_id, link)
    with pytest.raises(LiteratureError, match="immutable"):
        store.export(snapshot_id, store.path(snapshot_id) / "out.zip")


@pytest.mark.parametrize(
    "member",
    [
        "../escape",
        "/absolute",
        "resources\\evil",
        "resources//evil",
        "resources/./evil",
        "resources/a:b",
    ],
)
def test_unsafe_extra_archive_members_rejected_before_publication(
    tmp_path, document, resources, member
):
    source = LiteratureStore(tmp_path / "source")
    snapshot_id = source.publish(document, resources)
    archive = tmp_path / "source.zip"
    source.export(snapshot_id, archive)
    altered = _rewrite(archive, tmp_path / "unsafe.zip", extra=[(member, b"unsafe")])
    fresh = LiteratureStore(tmp_path / "fresh")
    with pytest.raises(LiteratureError, match="Unsafe"):
        fresh.import_bundle(altered)
    _assert_empty(fresh)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "mutation", ["hash", "size", "extra", "missing", "duplicate", "symlink", "directory"]
)
def test_corrupt_or_unexpected_members_rejected_and_staging_cleaned(
    tmp_path, document, resources, mutation
):
    source = LiteratureStore(tmp_path / "source")
    snapshot_id = source.publish(document, resources)
    archive = tmp_path / "source.zip"
    source.export(snapshot_id, archive)
    kwargs = {}
    if mutation == "hash":
        kwargs["mutate"] = lambda name, raw: (
            raw.replace(b"pinned", b"forged") if name.endswith("publications.json") else raw
        )
    elif mutation == "size":
        kwargs["mutate"] = lambda name, raw: (
            raw + b"bad" if name.endswith("publications.json") else raw
        )
    elif mutation == "extra":
        kwargs["extra"] = [("unlisted.json", b"{}")]
    elif mutation == "missing":
        kwargs["omit"] = ["resources/rc/publications.json"]
    elif mutation == "duplicate":
        kwargs["extra"] = [("document.json", b"{}")]
    else:
        info = zipfile.ZipInfo("symlink" if mutation == "symlink" else "directory/")
        info.external_attr = (
            (stat.S_IFLNK | 0o777) << 16 if mutation == "symlink" else (stat.S_IFDIR | 0o755) << 16
        )
        kwargs["extra"] = [(info, b"target")]
    altered = _rewrite(archive, tmp_path / "altered.zip", **kwargs)
    fresh = LiteratureStore(tmp_path / "fresh")
    with pytest.raises(LiteratureError):
        fresh.import_bundle(altered)
    _assert_empty(fresh)


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda m: m.update(schema_version=2), "Unsupported"),
        (lambda m: m["counts"].update(papers=99), "counts"),
        (lambda m: m["files"]["document.json"].update(bytes=10**9), "integrity"),
    ],
)
def test_redeclared_manifest_cannot_bypass_schema_counts_or_bounds(
    tmp_path, document, resources, change, match
):
    source = LiteratureStore(tmp_path / "source")
    snapshot_id = source.publish(document, resources)
    archive = tmp_path / "source.zip"
    source.export(snapshot_id, archive)
    altered = _redeclare_manifest(archive, tmp_path / "altered.zip", change)
    fresh = LiteratureStore(tmp_path / "fresh")
    with pytest.raises(LiteratureError, match=match):
        fresh.import_bundle(altered)
    _assert_empty(fresh)


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda d: d.update(schema_version=True), "schema_version"),
        (lambda d: d["papers"].append(copy.deepcopy(d["papers"][0])), "Duplicate"),
        (lambda d: d["papers"][0].pop("abstract"), "abstract"),
        (lambda d: d["papers"][0].update(abstract=42), "abstract"),
        (lambda d: d["papers"][0]["references"][0].update(title="Forged title"), "reference"),
        (lambda d: d["papers"][0]["references"][0].update(id=[]), "reference"),
        (lambda d: d["relationships"][0].update(paper_id="unknown"), "unknown"),
        (lambda d: d["relationships"][0].update(dataset_id=[]), "unknown"),
        (
            lambda d: d["relationships"][0].update(relationship_type="mentions_as_verified"),
            "relationship_type",
        ),
        (lambda d: d["relationships"][0].update(relationship_type=[]), "relationship_type"),
        (lambda d: d["relationships"][0]["evidence"].update(source_id="unknown"), "source"),
        (lambda d: d["relationships"][0]["review"].update(status="accepted"), "review status"),
        (lambda d: d["relationships"][0]["review"].update(reviewer=None), "reviewer"),
        (
            lambda d: d["relationships"][0]["review"].update(
                status="human_reviewed", reviewer="Agent"
            ),
            "human reviewer_type",
        ),
        (
            lambda d: d["relationships"][0]["review"].update(
                status="human_reviewed", reviewer="Agent", reviewer_type="human", method="agent"
            ),
            "Agent/generated",
        ),
        (lambda d: d["sources"][0].update(resource_paths=[[]]), "Unsafe"),
        (lambda d: d["sources"][0].update(resource_paths=["not-captured"]), "Missing source"),
        (lambda d: d.update(extra=float("nan")), "finite JSON"),
    ],
)
def test_invalid_document_never_publishes(tmp_path, document, resources, change, match):
    store = LiteratureStore(tmp_path)
    change(document)
    with pytest.raises(LiteratureError, match=match):
        store.publish(document, resources)
    _assert_empty(store)


def test_human_review_requires_explicit_identity_but_pending_generated_labels_survive(
    tmp_path, document, resources
):
    store = LiteratureStore(tmp_path)
    document["relationships"][1]["review"].update(method="agent")
    first = store.publish(document, resources)
    assert store.read(first)["relationships"][1]["review"]["status"] == "pending"
    document["relationships"][1]["review"] = {
        "status": "human_reviewed",
        "reviewer": "Named reviewer",
        "reviewer_type": "human",
        "method": "manual",
    }
    second = store.publish(document, resources)
    assert store.read(second)["relationships"][1]["review"]["reviewer_type"] == "human"
    assert store.read(first)["relationships"][1]["review"]["status"] == "pending"


def test_failed_publication_cleans_stage_and_preserves_old_snapshot(
    tmp_path, document, resources, monkeypatch
):
    store = LiteratureStore(tmp_path)
    first = store.publish(document, resources)
    document["papers"][0]["abstract"] = "Updated capture"
    monkeypatch.setattr(
        literature_store.os,
        "rename",
        lambda *args: (_ for _ in ()).throw(OSError("publication failed")),
    )
    with pytest.raises(OSError, match="publication failed"):
        store.publish(document, resources)
    assert store.list() == [
        {
            "snapshot_id": first,
            "counts": {"sources": 2, "papers": 2, "datasets": 2, "relationships": 3},
        }
    ]
    assert not list(store.snapshots.glob(".pending-*"))


def test_existing_corrupted_capture_is_detected_instead_of_overwritten(
    tmp_path, document, resources
):
    store = LiteratureStore(tmp_path)
    snapshot_id = store.publish(document, resources)
    target = store.path(snapshot_id) / "resources/rc/publications.json"
    target.write_bytes(b"corrupt")
    with pytest.raises(LiteratureError, match="size"):
        store.read(snapshot_id)
    with pytest.raises(LiteratureError, match="size"):
        store.publish(document, resources)
    assert target.read_bytes() == b"corrupt"
    assert not list(store.snapshots.glob(".pending-*"))


def test_expanded_document_round_trips_and_still_has_a_size_bound(
    tmp_path, document, resources, monkeypatch
):
    store = LiteratureStore(tmp_path / "source")
    document["capture_notes"] = "x" * (9 * 1024 * 1024)
    snapshot_id = store.publish(document, resources)
    assert store.verify(snapshot_id)["files"]["document.json"]["bytes"] > 8 * 1024 * 1024
    bundle = tmp_path / "expanded.zip"
    store.export(snapshot_id, bundle)
    fresh = LiteratureStore(tmp_path / "fresh")
    assert fresh.import_bundle(bundle) == snapshot_id
    assert fresh.read(snapshot_id) == store.read(snapshot_id)
    original_hash = hashlib.sha256(
        (store.path(snapshot_id) / "document.json").read_bytes()
    ).hexdigest()
    monkeypatch.setattr(literature_store, "MAX_DOCUMENT_BYTES", 8 * 1024 * 1024)
    with pytest.raises(LiteratureError, match="size budget"):
        store.publish(document, resources)
    assert (
        hashlib.sha256((store.path(snapshot_id) / "document.json").read_bytes()).hexdigest()
        == original_hash
    )


@pytest.mark.parametrize("change", ["extra", "symlink", "manifest"])
def test_local_snapshot_damage_is_visible(tmp_path, document, resources, change):
    store = LiteratureStore(tmp_path)
    snapshot_id = store.publish(document, resources)
    path = store.path(snapshot_id)
    if change == "extra":
        (path / "extra").write_text("unexpected")
    elif change == "symlink":
        target = path / "resources/rc/publications.json"
        target.unlink()
        target.symlink_to(tmp_path / "missing")
    else:
        manifest = json.loads((path / "manifest.json").read_bytes())
        manifest["counts"]["papers"] = 42
        (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(LiteratureError):
        store.verify(snapshot_id)


def test_raw_resource_path_conflicts_and_budget_fail_without_publication(
    tmp_path, document, resources, monkeypatch
):
    store = LiteratureStore(tmp_path)
    with pytest.raises(LiteratureError, match="conflict"):
        store.publish(document, {**resources, "rc": b"conflict"})
    monkeypatch.setattr(literature_store, "MAX_RESOURCE_BYTES", 1)
    with pytest.raises(LiteratureError, match="bounded"):
        store.publish(document, resources)
    _assert_empty(store)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": True},
        {"limit": 101},
        {"offset": -1},
        {"relationship_type": []},
        {"dataset_id": []},
        {"dataset_id": "missing"},
        {"query": None},
    ],
)
def test_invalid_search_filters_fail_clearly(tmp_path, document, resources, kwargs):
    store = LiteratureStore(tmp_path)
    snapshot_id = store.publish(document, resources)
    with pytest.raises(LiteratureError):
        store.search(snapshot_id, **kwargs)


def test_search_filter_total_and_pagination(tmp_path, document, resources):
    store = LiteratureStore(tmp_path)
    snapshot_id = store.publish(document, resources)
    first = store.search(snapshot_id, limit=1)
    second = store.search(snapshot_id, limit=1, offset=1)
    assert first["total"] == second["total"] == 2
    assert first["rows"][0]["paper"]["id"] != second["rows"][0]["paper"]["id"]
    assert store.search(snapshot_id, offset=9)["rows"] == []
    selected = store.search(snapshot_id, dataset_id="survey", relationship_type="inferred_mention")
    assert selected["total"] == 1
    assert selected["rows"][0]["relationships"][0]["relationship"]["id"] == "r3"
    assert store.search(snapshot_id, "NO MATCH")["total"] == 0
