"""Offline derived snapshots track every provider interpretation dependency."""

import hashlib

import pytest
from test_scotland import client_for, fixture

from ssdu.adapters import scotland
from ssdu.models import Request
from ssdu.store import Store, canonical


def test_scottish_semantic_constants_invalidate_historical_migration_cache(tmp_path, monkeypatch):
    import ssdu.store as store_module

    request, metadata, observations = fixture()
    request = Request(**request)
    store = Store(tmp_path)
    with client_for(metadata, observations) as client:
        acquired = scotland.fetch(request, client)
        acquired.dataset = None
        parent = store._publish(
            request,
            hashlib.sha256(canonical(request.to_dict())).hexdigest(),
            acquired,
            client.resources,
        )
    monkeypatch.setattr(
        "ssdu.http.HttpClient.request", lambda *a, **kw: pytest.fail("Migration attempted HTTP")
    )
    first = store.migrate_dataset(parent["product_id"])
    checksum = store_module.checksum
    adapter_path = scotland.__file__

    def changed(path):
        return "b" * 64 if str(path) == adapter_path else checksum(path)

    # Scotland's normalizer imports its RDF interpretation constants from this adapter.
    # Simulate a new implementation digest without editing any shared source file.
    monkeypatch.setattr(store_module, "checksum", changed)
    second = store.migrate_dataset(parent["product_id"])
    assert first.snapshot_id != second.snapshot_id
    assert first.to_arrow().equals(second.to_arrow())
    assert store.migrate_dataset(parent["product_id"]).snapshot_id == second.snapshot_id
    assert store.verify(second.snapshot_id)["implementation_sha256"]["scotland.py"] == "b" * 64
