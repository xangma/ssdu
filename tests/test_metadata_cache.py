"""Invented metadata responses exercise freshness independently of data values."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from ssdu.connector_cache import CachedClient, _canonical
from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter

URL = "https://www.nomisweb.co.uk/invented-catalogue"


def client(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        retries=0,
        rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
    )


def stamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


@pytest.mark.parametrize(
    ("response_header", "request_header", "value"),
    [
        ("ETag", "if-none-match", '"invented-v1"'),
        ("Last-Modified", "if-modified-since", "Mon, 12 Oct 2026 00:00:00 GMT"),
    ],
)
def test_conditional_refresh_retains_body_and_advances_only_check_time(
    tmp_path, response_header, request_header, value
):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200, json={"title": "Invented catalogue"}, headers={response_header: value}
            )
        assert request.headers[request_header] == value
        return httpx.Response(304, headers={response_header: value})

    with client(tmp_path, handler) as transport:
        with CachedClient(tmp_path, client=transport, clock=lambda: 100) as cache:
            original = cache.get(URL)
        with CachedClient(tmp_path, client=transport, clock=lambda: 150) as cache:
            warm = cache.get(URL)
            assert warm.cache_hit
            assert warm.captured_at == warm.checked_at == stamp(100)
        before_bytes = transport.total_bytes
        with CachedClient(tmp_path, client=transport, refresh=True, clock=lambda: 200) as cache:
            checked = cache.get(URL)
            assert checked.body == original.body
            assert checked.captured_at == stamp(100)
            assert checked.checked_at == stamp(200)
            assert checked.revalidated and not checked.cache_hit
        assert transport.total_bytes == before_bytes
        assert transport.attempts == 2
    with CachedClient(tmp_path, offline=True, clock=lambda: 300) as cache:
        replay = cache.get(URL)
        assert replay.cache_hit and replay.checked_at == stamp(200)
        assert replay.captured_at == stamp(100)


def test_changed_conditional_response_replaces_current_pointer_preserving_old_body(tmp_path):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"revision": count}, headers={"ETag": f'"{count}"'})

    with client(tmp_path, handler) as transport:
        with CachedClient(tmp_path, client=transport, clock=lambda: 100) as cache:
            first = cache.get(URL)
        with CachedClient(tmp_path, client=transport, refresh=True, clock=lambda: 200) as cache:
            second = cache.get(URL)
    assert first.body != second.body
    assert second.captured_at == second.checked_at == stamp(200)
    assert len(list((tmp_path / "connector-cache/objects").glob("*.bin"))) == 2


def test_legacy_cache_has_unknown_contact_time(tmp_path):
    import hashlib

    with (
        client(tmp_path, lambda _: httpx.Response(200, content=b"invented")) as transport,
        CachedClient(tmp_path, client=transport) as cache,
    ):
        cache.get(URL)
    path = next((tmp_path / "connector-cache").glob("*.json"))
    doc = json.loads(path.read_bytes())
    for key in ("capture_id", "captured_at", "checked_at"):
        doc.pop(key)
    doc["capture_id"] = hashlib.sha256(_canonical(doc)).hexdigest()
    path.write_bytes(_canonical(doc))
    with CachedClient(tmp_path, offline=True) as cache:
        resource = cache.get(URL)
    assert resource.body == b"invented"
    assert resource.captured_at is resource.checked_at is None


def test_unrequested_304_is_rejected(tmp_path):
    with (
        client(tmp_path, lambda _: httpx.Response(304)) as transport,
        CachedClient(tmp_path, client=transport) as cache,
        pytest.raises(RetrievalError),
    ):
        cache.get(URL)
