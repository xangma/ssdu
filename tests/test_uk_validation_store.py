import hashlib
import json

import pytest
from test_literature_validation import fixture

from ssdu.cli import main
from ssdu.literature_store import LiteratureError, LiteratureStore
from ssdu.uk_validation_store import prepare_inputs, publish_validation, replay_validation


def inputs(tmp_path):
    source, provider, reviews, resources = fixture()
    store = LiteratureStore(tmp_path / "source-store")
    sid = store.publish(source, {"article.json": resources["article.json"]})
    pid = store.publish(provider, {"catalogue.json": resources["catalogue.json"]})
    reviews[0]["source_snapshot_id"] = sid
    path = tmp_path / "review.json"
    path.write_text(json.dumps(reviews[0]))
    return store, sid, pid, path


def test_portable_review_recompiles_in_fresh_store_with_original_store_removed(
    tmp_path, monkeypatch
):
    import shutil
    import socket

    store, sid, pid, path = inputs(tmp_path)
    result = publish_validation(store, sid, pid, [path])
    identifier = result["snapshot_id"]
    document = store.read(identifier)
    assert document["relationships"][0]["relationship_type"] == "verified_use"
    assert store.search(identifier, dataset_id="deprivation:d2019")["total"] == 1
    assert document["validation_report"]["records"][0]["independently_human_validated"] is False
    archive = tmp_path / "review.zip"
    store.export(identifier, archive)
    fresh = LiteratureStore(tmp_path / "fresh")
    assert fresh.import_bundle(archive) == identifier
    shutil.rmtree(store.root)
    path.unlink()

    def deny(*args, **kwargs):
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    assert replay_validation(fresh, identifier)["identical_snapshot"] is True
    assert main(["--store", str(fresh.root), "literature", "revalidate-links", identifier]) == 0


@pytest.mark.parametrize("kind", ["hash", "escape", "duplicate-name"])
def test_rejects_unsafe_or_corrupt_review_inputs(tmp_path, kind):
    store, sid, pid, path = inputs(tmp_path)
    review = json.loads(path.read_text())
    raw = b"retained bytes"
    local = tmp_path / "evidence.txt"
    local.write_bytes(raw)
    review["resources"] = [
        {
            "path": "evidence.txt",
            "local_path": str(local),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    ]
    paths = [path]
    if kind == "hash":
        review["resources"][0]["sha256"] = "0" * 64
    elif kind == "escape":
        outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
        outside.write_bytes(raw)
        review["resources"][0]["local_path"] = str(outside)
    else:
        paths.append(path)
    path.write_text(json.dumps(review))
    with pytest.raises(LiteratureError):
        prepare_inputs(store, sid, pid, paths)


def test_rejected_candidate_is_preserved_in_audit_but_not_a_use_link(tmp_path):
    store, sid, pid, path = inputs(tmp_path)
    review = json.loads(path.read_text())
    row = review["records"][0]
    row.update(use_decision="rejected", resolutions=[], questions=[])
    path.write_text(json.dumps(review))
    result = publish_validation(store, sid, pid, [path])
    document = store.read(result["snapshot_id"])
    assert document["relationships"] == []
    assert document["validation_report"]["records"][0]["use_decision"] == "rejected"


def test_historical_release_is_searchable_without_inventing_provider_native_id(tmp_path):
    store, sid, pid, path = inputs(tmp_path)
    review = json.loads(path.read_text())
    resolution = review["records"][0]["resolutions"][0]
    resolution.update(dataset_id=None, native_id=None)
    path.write_text(json.dumps(review))
    result = publish_validation(store, sid, pid, [path])
    document = store.read(result["snapshot_id"])
    historical = [row for row in document["datasets"] if row["id"].startswith("reviewed-release:")]
    assert len(historical) == 1
    assert historical[0]["native_id"] is None
    assert historical[0]["version"] == "2019"
    assert historical[0]["retrieval_supported"] is False
    assert store.search(result["snapshot_id"], dataset_id=historical[0]["id"])["total"] == 1


def test_modified_frozen_document_cannot_be_rebound_to_original_snapshot_id(tmp_path):
    from ssdu.uk_validation_store import compile_validation

    store, sid, pid, path = inputs(tmp_path)
    prepared = prepare_inputs(store, sid, pid, [path])
    prepared["source_document"]["papers"][0]["abstract"] += " Modified."
    with pytest.raises(LiteratureError, match="original snapshot"):
        compile_validation(prepared)
