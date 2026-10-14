import hashlib

import pytest

from ssdu.models import RetrievalError
from ssdu.store import Store, canonical


def capture(store, number, parents=()):
    body = str(number).encode()
    identity = {
        "schema_version": 2,
        "created_at": "2026-10-13T00:00:00+00:00",
        "request": {"provider": "nomis", "dataset_id": "lineage-fixture", "params": {"n": number}},
        "parent_snapshot_ids": list(parents),
        "files": {"raw.bin": {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}},
    }
    product_id = hashlib.sha256(canonical(identity)).hexdigest()
    path = store.snapshots / product_id
    path.mkdir()
    (path / "raw.bin").write_bytes(body)
    (path / "manifest.json").write_bytes(canonical({**identity, "product_id": product_id}))
    return product_id


def test_shared_parent_verified_once_per_call(tmp_path, monkeypatch):
    import ssdu.store as module

    store = Store(tmp_path)
    parent = capture(store, 0)
    left = capture(store, 1, [parent])
    right = capture(store, 2, [parent])
    root = capture(store, 3, [left, right])
    original, calls = module.checksum, []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(module, "checksum", counted)
    assert store.verify(root)["product_id"] == root
    assert len(calls) == 4 and len(set(calls)) == 4
    (store.path(parent) / "raw.bin").write_bytes(b"bad")
    with pytest.raises(RetrievalError, match="hash mismatch"):
        store.verify(root)


def test_lineage_count_is_bounded(tmp_path):
    store = Store(tmp_path)
    parents = [capture(store, n) for n in range(128)]
    root = capture(store, 129, parents)
    with pytest.raises(RetrievalError, match="count budget"):
        store.verify(root)


def test_memoized_subtree_does_not_bypass_depth_bound(tmp_path):
    store = Store(tmp_path)
    subtree = capture(store, 0)
    for n in range(1, 5):
        subtree = capture(store, n, [subtree])
    deep = subtree
    for n in range(5, 9):
        deep = capture(store, n, [deep])
    root = capture(store, 9, [subtree, deep])
    with pytest.raises(RetrievalError, match="depth budget"):
        store.verify(root)
