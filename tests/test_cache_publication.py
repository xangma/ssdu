"""Explicit refresh repairs availability without changing damaged captures."""

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.shared import SharedDeprivation
from ssdu.store import Store


def client(body):
    return HttpClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))


def test_explicit_refresh_publishes_valid_copy_of_corrupt_dedup_candidate(tmp_path, iod_csv):
    shared = SharedDeprivation(Store(tmp_path))
    with client(iod_csv) as transport:
        original = shared.source(client=transport)
    original_path = shared.store.path(original["product_id"])
    original_manifest = (original_path / "manifest.json").read_bytes()
    damaged_bytes = b"damaged retained raw file"
    (original_path / "raw/0000.bin").write_bytes(damaged_bytes)

    # A frozen read must still fail; repair requires an explicit provider refresh.
    with pytest.raises(RetrievalError, match="hash mismatch"):
        shared.source(offline=True)
    with client(iod_csv) as transport:
        repaired = shared.source(refresh=True, client=transport)
        assert transport.attempts == 1

    assert repaired["product_id"] != original["product_id"]
    assert repaired["content_fingerprint"] == original["content_fingerprint"]
    assert shared.store.verify(repaired["product_id"])["rows"] == 4
    assert (shared.store.path(repaired["product_id"]) / "raw/0000.bin").read_bytes() == iod_csv
    assert shared.source(offline=True)["product_id"] == repaired["product_id"]
    assert (original_path / "manifest.json").read_bytes() == original_manifest
    assert (original_path / "raw/0000.bin").read_bytes() == damaged_bytes
    with pytest.raises(RetrievalError, match="hash mismatch"):
        shared.store.verify(original["product_id"])


def test_unrelated_malformed_manifest_does_not_block_fresh_source_publication(tmp_path, iod_csv):
    shared = SharedDeprivation(Store(tmp_path))
    with client(iod_csv) as transport:
        original = shared.source(client=transport)
    malformed_path = shared.store.snapshots / ("0" * 64)
    malformed_path.mkdir()
    malformed_bytes = b"{broken manifest"
    (malformed_path / "manifest.json").write_bytes(malformed_bytes)

    changed_bytes = iod_csv.replace(b"12.375", b"13.375")
    with client(changed_bytes) as transport:
        fresh = shared.source(refresh=True, client=transport)
        assert transport.attempts == 1

    assert fresh["product_id"] != original["product_id"]
    assert shared.store.verify(fresh["product_id"])["rows"] == 4
    assert shared.store.replay(fresh["product_id"]).loc[1, "imd_score"] == 13.375
    assert shared.store.replay(original["product_id"]).loc[1, "imd_score"] == 12.375
    assert shared.source(offline=True)["product_id"] == fresh["product_id"]
    assert (malformed_path / "manifest.json").read_bytes() == malformed_bytes
