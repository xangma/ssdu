"""Invented HTTP evidence tests: no provider observations or captured metadata."""

import hashlib
import json

import httpx
import pytest

from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient, ProviderRetry
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter

URL = "https://www.nomisweb.co.uk/synthetic-metadata"


def transport_client(handler):
    return HttpClient(transport=httpx.MockTransport(handler), retries=0)


def test_successful_responses_are_reusable_and_offline_is_network_free(tmp_path):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"synthetic": True})

    with transport_client(respond) as transport:
        with CachedClient(tmp_path, client=transport) as first:
            resource = first.get(URL, {"page": 1})
        with CachedClient(tmp_path, client=transport) as warm:
            assert warm.get(URL, {"page": 1}) == resource
        with CachedClient(tmp_path, offline=True) as offline:
            assert offline.get(URL, {"page": 1}) == resource
            assert offline.resources == [resource]
            with pytest.raises(RetrievalError, match="No retained"):
                offline.get(URL, {"page": 2})
    assert len(calls) == 1


def test_repeated_revision_checks_within_one_acquisition_are_not_hidden(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"revision": len(calls)})

    with (
        transport_client(respond) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        before = client.get(URL).json()
        after = client.get(URL).json()
    assert before != after
    assert len(calls) == 2


def test_failed_refresh_preserves_verified_prior_evidence(tmp_path):
    with (
        transport_client(lambda r: httpx.Response(200, content=b"invented-v1")) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        prior = client.get(URL)
    with (
        transport_client(lambda r: httpx.Response(503)) as transport,
        CachedClient(tmp_path, client=transport, refresh=True) as client,
        pytest.raises(ProviderRetry),
    ):
        client.get(URL)
    with CachedClient(tmp_path, offline=True) as client:
        assert client.get(URL) == prior


def test_successful_prefix_survives_interruption_without_refetch(tmp_path):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.params.get("page") == "2" and len(calls) == 2:
            raise KeyboardInterrupt("invented worker interruption")
        return httpx.Response(200, json={"page": request.url.params["page"]})

    with transport_client(respond) as transport:
        with CachedClient(tmp_path, client=transport) as client:
            client.get(URL, {"page": 1})
            with pytest.raises(KeyboardInterrupt):
                client.get(URL, {"page": 2})
        with CachedClient(tmp_path, client=transport) as resumed:
            assert resumed.get(URL, {"page": 1}).json() == {"page": "1"}
            assert resumed.get(URL, {"page": 2}).json() == {"page": "2"}
    assert len(calls) == 3


@pytest.mark.parametrize("part", ["body", "metadata", "missing-body"])
def test_corrupt_capture_fails_closed_without_implicit_network_repair(tmp_path, part):
    with (
        transport_client(lambda r: httpx.Response(200, content=b"invented")) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        client.get(URL)
    root = tmp_path / "connector-cache"
    metadata = next(root.glob("*.json"))
    document = json.loads(metadata.read_bytes())
    body = root / "objects" / f"{document['body_sha256']}.bin"
    if part == "body":
        body.write_bytes(b"corrupt")
    elif part == "metadata":
        document["url"] = "https://untrusted.invalid/private"
        metadata.write_text(json.dumps(document))
    else:
        body.unlink()
    with (
        CachedClient(tmp_path, offline=True) as client,
        pytest.raises(RetrievalError, match="verification failed"),
    ):
        client.get(URL)


def test_allowlist_is_enforced_before_offline_lookup(tmp_path):
    with CachedClient(tmp_path, offline=True) as client:
        for url in (
            "http://www.nomisweb.co.uk/a",
            "https://untrusted.invalid/a",
            "https://user:pass@www.nomisweb.co.uk/a",
        ):
            with pytest.raises(RetrievalError, match="allowlist"):
                client.get(url)


def test_post_body_is_part_of_identity(tmp_path):
    with (
        transport_client(lambda r: httpx.Response(200, content=r.content)) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        a = client.post(URL, json={"codes": ["001"]})
        b = client.post(URL, json={"codes": ["002"]})
    assert a.body != b.body
    with CachedClient(tmp_path, offline=True) as client:
        assert client.post(URL, json={"codes": ["001"]}) == a
        assert client.post(URL, json={"codes": ["002"]}) == b


def test_atomic_refresh_never_overwrites_prior_response_body(tmp_path):
    with (
        transport_client(lambda r: httpx.Response(200, content=b"invented-v1")) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        client.get(URL)
    with (
        transport_client(lambda r: httpx.Response(200, content=b"invented-v2")) as transport,
        CachedClient(tmp_path, client=transport, refresh=True) as client,
    ):
        client.get(URL)
    root = tmp_path / "connector-cache" / "objects"
    assert (
        root / f"{hashlib.sha256(b'invented-v1').hexdigest()}.bin"
    ).read_bytes() == b"invented-v1"
    with CachedClient(tmp_path, offline=True) as client:
        assert client.get(URL).body == b"invented-v2"


def test_cached_calls_do_not_reserve_rate_slots(tmp_path, monkeypatch):
    with transport_client(lambda r: httpx.Response(200, content=b"invented")) as transport:
        with CachedClient(tmp_path, client=transport) as client:
            client.get(URL)
        monkeypatch.setattr(RateLimiter, "acquire", lambda *a, **k: pytest.fail("cache used quota"))
        with CachedClient(tmp_path, client=transport) as client:
            client.get(URL)


def test_offline_refresh_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Offline"):
        CachedClient(tmp_path, offline=True, refresh=True)


def test_same_url_data_is_isolated_between_resolved_releases(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"version": len(calls)})

    with transport_client(respond) as transport:
        with CachedClient(tmp_path, client=transport) as client, client.scope("release-v1"):
            old = client.get(URL)
        with CachedClient(tmp_path, client=transport) as client, client.scope("release-v2"):
            new = client.get(URL)
    assert old.body != new.body
    assert len(calls) == 2
    with CachedClient(tmp_path, offline=True) as client:
        with client.scope("release-v1"):
            assert client.get(URL) == old
        with client.scope("release-v2"):
            assert client.get(URL) == new


def test_replayed_metadata_checkpoint_still_causes_online_final_revision_check(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"revision": len(calls)})

    with transport_client(respond) as transport:
        with CachedClient(tmp_path, client=transport) as client:
            before = client.get(URL)
        with CachedClient(tmp_path, client=transport) as resumed:
            resumed.mark_replayed(before)
            after = resumed.get(URL)
    assert after.body != before.body
    assert len(calls) == 2


@pytest.mark.parametrize("client_kind", ["online_cache", "custom_persistent", "claims_offline"])
def test_store_offline_policy_cannot_be_overridden_by_injected_client(tmp_path, client_kind):
    from types import SimpleNamespace

    from ssdu.adapters.nomis import default_request
    from ssdu.store import Store

    def forbidden(*args, **kwargs):
        pytest.fail("Store offline acquisition contacted a provider")

    store = Store(tmp_path)
    with transport_client(forbidden) as transport:
        injected = (
            CachedClient(tmp_path, client=transport)
            if client_kind == "online_cache"
            else SimpleNamespace(
                persistent_cache=True,
                offline=client_kind == "claims_offline",
                resources=[],
                request=forbidden,
            )
        )
        with pytest.raises(RetrievalError, match="No retained"):
            store.fetch(default_request(), offline=True, client=injected)
    assert store.list() == []


def test_unrelated_damaged_snapshot_does_not_block_healthy_offline_metadata(tmp_path, monkeypatch):
    import shutil

    from test_mhclg import client_for, publication

    from ssdu import Connector, Request, Store
    from ssdu.connector_cache import seed_from_snapshots

    store = Store(tmp_path)
    with client_for(publication("healthy-housing")) as client:
        healthy = store.fetch(Request("mhclg", "healthy-housing"), client=client)
    with client_for(publication("damaged-finance")) as client:
        damaged = store.fetch(Request("mhclg", "damaged-finance"), client=client)
    (store.path(damaged["product_id"]) / "data.parquet").write_bytes(b"corrupt")
    shutil.rmtree(store.root / "connector-cache")
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Offline used HTTP"))
    report = seed_from_snapshots(store)
    assert any(item["product_id"] == damaged["product_id"] for item in report["skipped_snapshots"])
    description = Connector("mhclg", store).describe_dataset("healthy-housing", offline=True)
    assert description["dataset_id"] == "healthy-housing"
    assert store.verify(healthy["product_id"])["dataset_id"] == "healthy-housing"


@pytest.mark.parametrize("relative", ["unhashed.bin", "../outside.bin", "/outside.bin"])
def test_historical_unhashed_resources_never_become_verified_cache_evidence(tmp_path, relative):
    from ssdu.connector_cache import seed_from_snapshots
    from ssdu.store import Store, canonical

    store = Store(tmp_path)
    identity = {
        "schema_version": 1,
        "provider": "nomis",
        "dataset_id": "NM_999_1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "request": {"provider": "nomis", "dataset_id": "NM_999_1", "params": {}},
        "files": {},
        "resources": [
            {
                "file": relative,
                "method": "GET",
                "url": URL,
                "request_body": None,
                "content_type": "application/json",
                "response_headers": {},
            }
        ],
    }
    snapshot_id = hashlib.sha256(canonical(identity)).hexdigest()
    folder = store.snapshots / snapshot_id
    folder.mkdir()
    (folder / "manifest.json").write_bytes(canonical({**identity, "product_id": snapshot_id}))
    (folder / "unhashed.bin").write_bytes(b'{"invented_unverified":true}')
    # Older manifest verification does not bind resources to files; the new cache must.
    assert store.verify(snapshot_id)["files"] == {}
    report = seed_from_snapshots(store)
    assert report["seeded"] == 0
    assert report["skipped_snapshots"][0]["product_id"] == snapshot_id
    with (
        CachedClient(tmp_path, offline=True) as client,
        pytest.raises(RetrievalError, match="No retained"),
    ):
        client.get(URL)
