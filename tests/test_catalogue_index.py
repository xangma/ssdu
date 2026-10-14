import copy
import json

import pytest

from ssdu.cli import main
from ssdu.literature_store import LiteratureStore
from ssdu.registry import Registry


def snapshot(root, ids=("NM_999_1",), *, bad_references=False):
    source = {
        "id": "nomis-metadata",
        "title": "Nomis definition",
        "url": "https://www.nomisweb.co.uk/api/v01/dataset/def.sdmx.json",
        "resource_paths": ["definitions.json"],
    }
    ref = {k: source[k] for k in ("id", "title", "url")}
    datasets, descriptors = [], []
    for native in ids:
        key = "nomis:" + native
        datasets.append(
            {
                "id": key,
                "title": "Education and employment",
                "description": "Official metadata",
                "provider": "nomis",
                "native_id": native,
                "doi": None,
                "version": None,
                "coverage": {},
                "source_ids": [source["id"]],
                "references": [ref],
            }
        )
        descriptors.append(
            {
                "dataset_id": key,
                "title": "Education and employment",
                "description": "Official metadata",
                "provider": "nomis",
                "native_id": native,
                "columns": [],
                "references": [ref],
                "metadata_scope": "Provider metadata harvest",
            }
        )
    if bad_references:
        descriptors[-1]["references"] = []
    doc = {
        "schema_version": 1,
        "sources": [source],
        "papers": [],
        "relationships": [],
        "datasets": datasets,
        "catalogue_descriptors": descriptors,
        "coverage_report": {"nomis": {"complete": True}},
    }
    store = LiteratureStore(root)
    return store, store.publish(doc, {"definitions.json": json.dumps(doc).encode()})


def test_import_is_idempotent_searchable_referenced_and_not_materialised(tmp_path, capsys):
    store, sid = snapshot(tmp_path)
    registry = Registry(tmp_path)
    assert registry.import_catalogue(store, sid)["indexed"] == 1
    assert Registry(tmp_path).import_catalogue(store, sid)["reused"]
    result = Registry(tmp_path).get("nomis:NM_999_1")
    assert result["harvest_snapshot_id"] == sid and result["references"]
    assert result["availability"]["catalogued"] and not result["availability"]["materialised"]
    assert main(["--store", str(tmp_path), "registry", "search", "education"]) == 0
    rows = json.loads(capsys.readouterr().out)["items"]
    assert len(rows) == 1 and rows[0]["references"] and rows[0]["harvest_snapshot_id"] == sid


def test_harvest_preserves_existing_custom_metadata_and_retains_new_version(tmp_path):
    store, sid = snapshot(tmp_path)
    registry = Registry(tmp_path)
    manual = copy.deepcopy(store.read(sid)["catalogue_descriptors"][0])
    manual.update(title="User reviewed interpretation", metadata_scope="User curation")
    with registry.connect() as db:
        version = registry._upsert(db, manual)
    result = registry.import_catalogue(store, sid)
    assert result["preserved_current_metadata"] == 1
    assert registry.get("nomis:NM_999_1")["metadata_version"] == version
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM catalogue_versions").fetchone()[0] == 1


def test_catalogue_validation_and_transaction_failure_leave_no_partial_index(tmp_path, monkeypatch):
    store, sid = snapshot(tmp_path, ("NM_999_1", "NM_998_1"))
    registry = Registry(tmp_path)
    original = registry._upsert
    calls = 0

    def fail_second(db, document, **kwargs):
        nonlocal calls
        calls += 1
        value = original(db, document, **kwargs)
        if calls == 2:
            raise RuntimeError("Injected publication failure")
        return value

    monkeypatch.setattr(registry, "_upsert", fail_second)
    with pytest.raises(RuntimeError, match="Injected"):
        registry.import_catalogue(store, sid)
    assert registry.search("education")["total"] == 0
    with registry.connect() as db:
        assert db.execute("SELECT count(*) FROM catalogue_imports").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM catalogue_versions").fetchone()[0] == 0
    _, bad = snapshot(tmp_path, bad_references=True)
    with pytest.raises(ValueError, match="source references"):
        registry.import_catalogue(store, bad)


def test_bundle_import_rebuilds_discovery_index_in_fresh_store(tmp_path, capsys):
    source = tmp_path / "source"
    store, sid = snapshot(source)
    package = tmp_path / "catalogue.zip"
    store.export(sid, package)
    destination = tmp_path / "fresh"
    assert main(["--store", str(destination), "harvest", "import", str(package)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["snapshot_id"] == sid and result["registry"]["indexed"] == 1
    assert Registry(destination).get("nomis:NM_999_1")["references"]
