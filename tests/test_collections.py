import copy
import hashlib
import json

import pandas as pd
import pytest
from pydantic import ValidationError

from ssdu.collection_models import CollectionLock
from ssdu.collections import Collections, read_document
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.store import Store, canonical


@pytest.fixture
def collection_store(tmp_path, monkeypatch):
    class Adapter:
        @staticmethod
        def fetch(request, client):
            return Acquisition(
                request.provider,
                request.dataset_id,
                "Fixture",
                pd.DataFrame({"code": ["001", "002"], "value": [1, 2]}),
                {"notes": "Distinct codes"},
                {"edition": "fixture"},
                "Fixture licence",
                "Fixture citation",
                "https://www.nomisweb.co.uk/fixture",
            )

    monkeypatch.setattr("ssdu.store.adapter", lambda _: Adapter)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    store = Store(tmp_path / "store")
    saved = store.fetch(Request("nomis", "fixture"))
    return store, saved


def spec(snapshot_id, **query):
    return {"name": "example", "entries": {"areas": {"snapshot_id": snapshot_id, "query": query}}}


def resign(document):
    document["lock_id"] = hashlib.sha256(
        canonical({k: v for k, v in document.items() if k != "lock_id"})
    ).hexdigest()
    return document


def test_collection_pins_inputs_metadata_and_results_offline(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(spec(saved["product_id"], columns=["code"], limit=1), offline=True)
    result = collections.replay("example")
    assert result == collections.replay(lock.lock_id) == collections.replay(lock)
    entry = result["entries"]["areas"]
    assert entry["rows"] == [{"code": "001"}]
    assert entry["total_rows"] == 2 and entry["truncated"]
    assert entry["provenance"]["metadata_version"] == lock.entries["areas"].metadata_version
    assert collections.list()[0]["current_lock_id"] == lock.lock_id
    assert store.root.as_posix() not in canonical(lock.model_dump()).decode()


def test_new_lock_preserves_old_lock_and_pinned_replay(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    old = collections.resolve(spec(saved["product_id"], count_only=True), offline=True)
    before = (collections.root / "example" / "locks" / f"{old.lock_id}.json").read_bytes()
    new = collections.resolve(
        spec(saved["product_id"], filters=[{"column": "value", "value": 2}]), offline=True
    )
    assert old.lock_id != new.lock_id
    assert collections.get("example").lock_id == new.lock_id
    assert collections.replay(old.lock_id)["entries"]["areas"]["rows"] == [{"count": 2}]
    assert collections.replay(new)["entries"]["areas"]["rows"] == [{"code": "002", "value": 2}]
    assert before == (collections.root / "example" / "locks" / f"{old.lock_id}.json").read_bytes()
    assert not collections.install_lock(old)["current"]
    assert collections.install_lock(old, activate=True)["current"]
    assert collections.get("example").lock_id == old.lock_id


def test_unchanged_resolution_reuses_current_lock(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    intent = spec(saved["product_id"])
    first = collections.resolve(intent, offline=True)
    assert collections.resolve(intent, offline=True).lock_id == first.lock_id
    assert collections.list()[0]["lock_ids"] == [first.lock_id]


def test_lock_detects_edits_and_rejects_changed_expected_results(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(spec(saved["product_id"]), offline=True)
    changed = copy.deepcopy(lock.model_dump())
    changed["entries"]["areas"]["result_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="lock hash mismatch"):
        CollectionLock.model_validate(changed)
    resign(changed)
    with pytest.raises(RetrievalError, match="query result changed"):
        collections.install_lock(changed, activate=True)
    assert collections.get("example").lock_id == lock.lock_id
    assert len(collections.list()[0]["lock_ids"]) == 1


def test_metadata_hash_and_source_evidence_are_both_verified(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(spec(saved["product_id"]), offline=True)
    changed = copy.deepcopy(lock.model_dump())
    entry = changed["entries"]["areas"]
    entry["metadata"]["licence"] = "Invented licence"
    with pytest.raises(ValidationError, match="metadata hash mismatch"):
        CollectionLock.model_validate(resign(changed))
    entry["metadata_version"] = hashlib.sha256(canonical(entry["metadata"])).hexdigest()
    with pytest.raises(RetrievalError, match="source evidence"):
        collections.install_lock(resign(changed))


def test_replay_uses_pinned_interpretation_after_registry_curation_changes(
    collection_store, monkeypatch
):
    import ssdu.service as service_module

    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(spec(saved["product_id"]), offline=True)
    before = collections.replay(lock)
    original = service_module.snapshot_descriptor

    def revised(*args):
        return {**original(*args), "description": "Later curation"}

    monkeypatch.setattr(service_module, "snapshot_descriptor", revised)
    assert collections.replay(lock) == before


def test_pinned_replay_does_not_depend_on_mutable_request_or_source_indexes(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(spec(saved["product_id"]), offline=True)
    before = collections.replay(lock)
    (store.root / "latest.json").write_text("broken index")
    (store.root / "sources.json").write_text("broken index")
    assert collections.replay(lock) == before


def test_install_failure_preserves_previous_lock_and_pointer(collection_store, monkeypatch):
    import ssdu.collections as module

    store, saved = collection_store
    collections = Collections(store)
    old = collections.resolve(spec(saved["product_id"]), offline=True)
    changed = copy.deepcopy(old.model_dump())
    changed["created_at"] = "2026-10-14T00:00:00+00:00"
    new = CollectionLock.model_validate(resign(changed))
    original = module._atomic_json

    def fail_pointer(path, value):
        if path.name == "current.json":
            raise OSError("Injected pointer publication failure")
        return original(path, value)

    monkeypatch.setattr(module, "_atomic_json", fail_pointer)
    with pytest.raises(OSError, match="Injected"):
        collections.install_lock(new, activate=True)
    assert collections.get("example").lock_id == old.lock_id
    assert collections.list()[0]["lock_ids"] == [old.lock_id]
    assert not (collections.root / "example" / f"catalog-{new.lock_id}.yaml").exists()


def test_safe_documents_and_unsafe_collection_names(tmp_path, collection_store):
    path = tmp_path / "bad.yaml"
    path.write_text("!!python/object/apply:os.system ['false']")
    with pytest.raises(ValueError, match="Invalid JSON/YAML"):
        read_document(path)
    path.write_text("x: " + "a" * 2_000_000)
    with pytest.raises(ValueError, match="2 MB"):
        read_document(path)
    store, saved = collection_store
    bad = spec(saved["product_id"])
    bad["name"] = "../outside"
    with pytest.raises(ValidationError):
        Collections(store).resolve(bad, offline=True)


def test_request_resolution_is_exact_and_offline(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(
        {"name": "by-request", "entries": {"areas": {"request": saved["request"]}}}, offline=True
    )
    assert lock.entries["areas"].snapshot_id == saved["product_id"]
    with pytest.raises(ValueError, match="cannot refresh"):
        collections.resolve(spec(saved["product_id"]), offline=True, refresh=True)


def test_tampered_pointer_cannot_substitute_collection(collection_store):
    store, saved = collection_store
    collections = Collections(store)
    lock = collections.resolve(spec(saved["product_id"]), offline=True)
    pointer = collections.root / "example" / "current.json"
    pointer.write_text(json.dumps({"lock_id": "../../outside"}))
    with pytest.raises(RetrievalError, match="current pointer"):
        collections.get("example")
    assert collections.get(lock.lock_id).lock_id == lock.lock_id
