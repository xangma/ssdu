import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pandas as pd
import pytest

from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.store import Store, canonical


@pytest.fixture
def fixture_adapter(monkeypatch):
    class Adapter:
        calls = 0

        @classmethod
        def fetch(cls, request, client):
            cls.calls += 1
            raw = client.get("https://www.nomisweb.co.uk/example").json()
            return Acquisition(
                "nomis",
                "fixture",
                "Fixture",
                pd.DataFrame(raw),
                {"flags": {"S": "suppressed"}},
                {"kind": "observed_snapshot"},
                "OGL",
                "Test fixture",
                "https://www.nomisweb.co.uk/example",
            )

    monkeypatch.setattr("ssdu.store.adapter", lambda _: Adapter)
    return Adapter


def client(value="1"):
    return HttpClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "code": ["001", "002"],
                    "value": [value, ".."],
                    "status": ["A", "S"],
                },
            )
        )
    )


def test_offline_cache_intake_and_refresh_keep_old_snapshot(tmp_path, fixture_adapter, monkeypatch):
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with client() as c:
        old = store.fetch(request, client=c)
    old_id = old["product_id"]
    original_bytes = (store.path(old_id) / "data.parquet").read_bytes()

    def no_network(*args, **kwargs):
        pytest.fail("Frozen replay/cache hit attempted HTTP")

    monkeypatch.setattr(HttpClient, "request", no_network)
    assert store.fetch(request)["product_id"] == old_id
    pd.testing.assert_frame_equal(store.replay(old_id), store.read_intake(old_id))
    assert store.replay(old_id)["code"].tolist() == ["001", "002"]
    assert fixture_adapter.calls == 1
    monkeypatch.undo()
    monkeypatch.setattr("ssdu.store.adapter", lambda _: fixture_adapter)
    with client("2") as c:
        new = store.fetch(request, refresh=True, client=c)
    assert new["product_id"] != old_id
    assert (store.path(old_id) / "data.parquet").read_bytes() == original_bytes
    assert store.replay(old_id).iloc[0]["value"] == "1"
    assert store.replay(new["product_id"]).iloc[0]["value"] == "2"


def test_corruption_fails_without_silent_refresh(tmp_path, fixture_adapter):
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with client() as c:
        saved = store.fetch(request, client=c)
    (store.path(saved["product_id"]) / "raw/0000.bin").write_bytes(b"corrupted")
    with pytest.raises(RetrievalError, match="hash mismatch"):
        store.fetch(request)
    assert fixture_adapter.calls == 1


def test_manifest_tampering_detected(tmp_path, fixture_adapter):
    store = Store(tmp_path)
    with client() as c:
        saved = store.fetch(Request("nomis", "fixture"), client=c)
    path = store.path(saved["product_id"]) / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["licence"] = "Invented rights"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RetrievalError, match="Manifest hash mismatch"):
        store.verify(saved["product_id"])


def test_acquisition_failure_publishes_nothing(tmp_path, monkeypatch):
    class Broken:
        def fetch(self, request, client):
            raise RetrievalError("Missing second page")

    monkeypatch.setattr("ssdu.store.adapter", lambda _: Broken())
    store = Store(tmp_path)
    with client() as c, pytest.raises(RetrievalError, match="second page"):
        store.fetch(Request("nomis", "fixture"), client=c)
    assert store.list() == []
    assert not (tmp_path / "latest.json").exists()


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError, match="Snapshot IDs"):
        Store(tmp_path).verify("../../etc/passwd")


def test_cache_index_cannot_substitute_another_request(tmp_path, fixture_adapter):
    store = Store(tmp_path)
    original = Request("nomis", "fixture", {"geography": "001"})
    other = Request("nomis", "fixture", {"geography": "002"})
    with client() as c:
        saved = store.fetch(original, client=c)
    wrong_key = hashlib.sha256(canonical(other.to_dict())).hexdigest()
    (tmp_path / "latest.json").write_text(json.dumps({wrong_key: saved["product_id"]}))
    with pytest.raises(RetrievalError, match="different request"):
        store.fetch(other)


def test_lost_index_recovers_most_recent_exact_capture_without_http(
    tmp_path, fixture_adapter, monkeypatch
):
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with client() as c:
        old = store.fetch(request, client=c)
    with client("2") as c:
        latest = store.fetch(request, refresh=True, client=c)
    (tmp_path / "latest.json").unlink()
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    assert store.fetch(request)["product_id"] == latest["product_id"]
    assert store.fetch(request, offline=True)["product_id"] == latest["product_id"]
    assert store.replay(old["product_id"]).iloc[0]["value"] == "1"
    assert fixture_adapter.calls == 2


def test_offline_miss_and_invalid_policy_never_contact_provider(
    tmp_path, fixture_adapter, monkeypatch
):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with pytest.raises(RetrievalError, match="exact request"):
        store.fetch(request, offline=True)
    with pytest.raises(ValueError, match="cannot refresh"):
        store.fetch(request, offline=True, refresh=True)
    assert fixture_adapter.calls == 0


def test_rebuild_preserves_valid_explicit_pointer_and_all_snapshot_bytes(tmp_path, fixture_adapter):
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with client() as c:
        old = store.fetch(request, client=c)
    with client("2") as c:
        newer = store.fetch(request, refresh=True, client=c)
    key = hashlib.sha256(canonical(request.to_dict())).hexdigest()
    store._update_index(key, old["product_id"])
    before = {str(p): p.read_bytes() for p in store.snapshots.rglob("*") if p.is_file()}
    report = store.rebuild_index()
    assert report["preserved_entries"] == 1 and report["recovered_entries"] == 0
    assert store.fetch(request, offline=True)["product_id"] == old["product_id"]
    assert store.verify(newer["product_id"])
    assert before == {str(p): p.read_bytes() for p in store.snapshots.rglob("*") if p.is_file()}


def test_bad_index_repair_reports_corruption_and_never_rewrites_it(tmp_path, fixture_adapter):
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with client() as c:
        old = store.fetch(request, client=c)
    with client("2") as c:
        intact = store.fetch(request, refresh=True, client=c)
    broken = store.path(old["product_id"]) / "raw/0000.bin"
    broken.write_bytes(b"damaged")
    (tmp_path / "latest.json").write_text("[]")
    with pytest.raises(RetrievalError, match="Invalid request index"):
        store.fetch(request, offline=True)
    status = store.cache_status(verify=True)
    assert status["verified_count"] == 1 and status["index_errors"]
    repaired = store.rebuild_index()
    assert repaired["repaired_index_errors"] and not repaired["index_errors"]
    assert repaired["invalid_snapshots"][0]["product_id"] == old["product_id"]
    assert broken.read_bytes() == b"damaged"
    assert store.fetch(request, offline=True)["product_id"] == intact["product_id"]


def test_lost_index_does_not_silently_replace_corrupt_newest_capture(
    tmp_path, fixture_adapter, monkeypatch
):
    store = Store(tmp_path)
    request = Request("nomis", "fixture")
    with client() as c:
        store.fetch(request, client=c)
    with client("2") as c:
        latest = store.fetch(request, refresh=True, client=c)
    (store.path(latest["product_id"]) / "raw/0000.bin").write_bytes(b"damaged")
    (tmp_path / "latest.json").unlink()
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    with pytest.raises(RetrievalError, match="hash mismatch"):
        store.fetch(request, offline=True)


def test_concurrent_identical_cache_misses_acquire_once(tmp_path, fixture_adapter):
    gate = Barrier(4)
    request = Request("nomis", "fixture")

    def fetch(_):
        gate.wait(timeout=5)
        with client() as c:
            return Store(tmp_path).fetch(request, client=c)["product_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(fetch, range(4)))
    assert len(set(ids)) == 1
    assert fixture_adapter.calls == 1
