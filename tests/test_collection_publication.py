"""Regression checks for collection publication boundaries and pinned metadata."""

import copy
import hashlib
import shutil
import threading

import pandas as pd
import pytest

import ssdu.collections as collection_module
from ssdu.bundles import export_bundle, import_bundle
from ssdu.collection_models import CollectionLock
from ssdu.collections import Collections
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.registry import Registry
from ssdu.store import Store, canonical


def resign(document):
    document["lock_id"] = hashlib.sha256(
        canonical({key: value for key, value in document.items() if key != "lock_id"})
    ).hexdigest()
    return CollectionLock.model_validate(document)


@pytest.fixture
def published_collection(tmp_path):
    source = Store(tmp_path / "source")
    request = Request("local", "fixture")
    acquisition = Acquisition(
        "local",
        "fixture",
        "Fixture",
        pd.DataFrame({"code": ["001"], "value": [1]}),
        {"description": "Original interpretation"},
        {},
        "Fixture licence",
        "Fixture citation",
        "urn:fixture",
    )
    saved = source._publish(
        request, hashlib.sha256(canonical(request.to_dict())).hexdigest(), acquisition, []
    )
    lock = Collections(source).resolve(
        {"name": "example", "entries": {"areas": {"snapshot_id": saved["product_id"]}}},
        offline=True,
    )
    return source, lock


@pytest.mark.parametrize("has_current", [False, True], ids=["fresh", "existing-current"])
def test_interrupt_after_pointer_replace_restores_complete_previous_state(
    published_collection, tmp_path, monkeypatch, has_current
):
    source, old = published_collection
    destination = Store(tmp_path / "destination")
    snapshot_id = old.entries["areas"].snapshot_id
    shutil.copytree(source.path(snapshot_id), destination.snapshots / snapshot_id)
    manager = Collections(destination)
    if has_current:
        manager.install_lock(old, activate=True)
    document = copy.deepcopy(old.model_dump())
    document["created_at"] = "2026-10-14T00:00:00+00:00"
    incoming = resign(document)
    original = collection_module._atomic_json
    interrupted = False

    def interrupt_after_replace(path, value):
        nonlocal interrupted
        original(path, value)
        if path.name == "current.json" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("Signal immediately after successful pointer replacement")

    monkeypatch.setattr(collection_module, "_atomic_json", interrupt_after_replace)
    with pytest.raises(KeyboardInterrupt, match="successful pointer replacement"):
        manager.install_lock(incoming, activate=True)
    assert interrupted
    directory = manager.root / "example"
    assert not (directory / "locks" / f"{incoming.lock_id}.json").exists()
    assert not (directory / f"catalog-{incoming.lock_id}.yaml").exists()
    if has_current:
        assert manager.get("example").lock_id == old.lock_id
        assert manager.replay("example") == Collections(source).replay(old)
    else:
        assert not (directory / "current.json").exists()
        assert manager.list() == []


def test_install_existing_capture_preserves_curated_current_metadata(published_collection):
    source, lock = published_collection
    manager = Collections(source)
    registry = Registry(source.root)
    metadata = {**lock.entries["areas"].metadata, "description": "Curated current interpretation"}
    with registry.connect() as db:
        curated_version = registry._upsert(db, metadata)
    key = lock.entries["areas"].dataset_id
    before = registry.get(key)
    assert before["metadata_version"] == curated_version
    manager.install_lock(lock)
    after = registry.get(key)
    assert after["metadata_version"] == curated_version
    assert after["description"] == "Curated current interpretation"
    assert after["current_snapshot_id"] == before["current_snapshot_id"]
    replayed = manager.replay(lock)["entries"]["areas"]
    assert replayed["metadata"]["description"] == "Original interpretation"
    assert replayed["provenance"]["metadata_version"] == lock.entries["areas"].metadata_version


def test_concurrent_collection_install_waits_for_failed_bundle_publication(
    published_collection, tmp_path, monkeypatch
):
    source, incoming = published_collection
    document = copy.deepcopy(incoming.model_dump())
    document["spec"]["name"] = "concurrent"
    concurrent = resign(document)
    archive = tmp_path / "bundle.zip"
    export_bundle(source, incoming, archive)
    destination = Store(tmp_path / "destination")
    original = Collections.install_lock
    started, completed = threading.Event(), threading.Event()
    outcome = []
    workers = []
    completed_before_rollback = []

    def concurrent_installer():
        started.set()
        try:
            original(Collections(destination), concurrent, activate=True)
            outcome.append("committed")
        except (RetrievalError, OSError, ValueError) as exc:
            outcome.append(exc)
        finally:
            completed.set()

    def interrupt_import_install(manager, lock, **kwargs):
        if manager.store.root == destination.root and lock.lock_id == incoming.lock_id:
            worker = threading.Thread(target=concurrent_installer, daemon=True)
            workers.append(worker)
            worker.start()
            assert started.wait(5)
            # The importer holds its publication barrier while captures are
            # visible. Another collection must not commit against them yet.
            completed_before_rollback.append(completed.wait(1))
            raise RuntimeError("Injected import failure before collection installation")
        return original(manager, lock, **kwargs)

    monkeypatch.setattr(Collections, "install_lock", interrupt_import_install)
    try:
        with pytest.raises(RetrievalError, match="could not be imported"):
            import_bundle(destination, archive)
    finally:
        for worker in workers:
            worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    assert completed_before_rollback == [False]
    assert len(outcome) == 1 and isinstance(outcome[0], (RetrievalError, OSError, ValueError))
    assert not list(destination.snapshots.iterdir())
    assert Collections(destination).list() == []


def test_interrupt_after_completed_install_keeps_committed_collection_usable(
    published_collection, tmp_path, monkeypatch
):
    source, lock = published_collection
    archive = tmp_path / "bundle.zip"
    export_bundle(source, lock, archive)
    destination = Store(tmp_path / "destination")
    original = Collections.install_lock

    def interrupt_after_commit(manager, incoming, **kwargs):
        result = original(manager, incoming, **kwargs)
        if manager.store.root == destination.root:
            raise KeyboardInterrupt("Signal after completed collection installation")
        return result

    monkeypatch.setattr(Collections, "install_lock", interrupt_after_commit)
    with pytest.raises(KeyboardInterrupt, match="completed collection installation"):
        import_bundle(destination, archive)
    assert Collections(destination).get("example").lock_id == lock.lock_id
    assert Collections(destination).replay("example") == Collections(source).replay(lock)
    for entry in lock.entries.values():
        destination.verify(entry.snapshot_id)


@pytest.mark.parametrize("index_mode", ["import_store", "register"])
def test_registry_indexing_waits_for_failed_bundle_publication(
    published_collection, tmp_path, monkeypatch, index_mode
):
    source, incoming = published_collection
    archive = tmp_path / "bundle.zip"
    export_bundle(source, incoming, archive)
    destination = Store(tmp_path / "destination")
    registry = Registry(destination.root)
    entry = incoming.entries["areas"]
    source_manifest = source.verify(entry.snapshot_id)
    # The dataset is discoverable before any capture has been imported.
    with registry.connect() as db:
        registry._upsert(db, entry.metadata)
    original = Collections.install_lock
    started, completed = threading.Event(), threading.Event()
    results, workers, completed_before_rollback = [], [], []

    def index_in_parallel():
        started.set()
        try:
            results.append(
                registry.import_store(destination)
                if index_mode == "import_store"
                else registry.register(destination, source_manifest)
            )
        except (RetrievalError, OSError, ValueError) as exc:
            results.append(exc)
        finally:
            completed.set()

    def interrupt_import_install(manager, lock, **kwargs):
        if manager.store.root == destination.root and lock.lock_id == incoming.lock_id:
            worker = threading.Thread(target=index_in_parallel, daemon=True)
            workers.append(worker)
            worker.start()
            assert started.wait(5)
            completed_before_rollback.append(completed.wait(1))
            raise RuntimeError("Injected import failure while another thread indexes captures")
        return original(manager, lock, **kwargs)

    monkeypatch.setattr(Collections, "install_lock", interrupt_import_install)
    try:
        with pytest.raises(RetrievalError, match="could not be imported"):
            import_bundle(destination, archive)
    finally:
        for worker in workers:
            worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    assert completed_before_rollback == [False]
    if index_mode == "import_store":
        assert results == [0]
    else:
        assert len(results) == 1 and isinstance(results[0], RetrievalError)
    assert not list(destination.snapshots.iterdir())
    observed = registry.get(entry.dataset_id)
    assert observed["current_snapshot_id"] is None
    assert observed["snapshots"] == []
    assert observed["availability"]["materialised"] is False
    assert observed["availability"]["queryable"] is False
