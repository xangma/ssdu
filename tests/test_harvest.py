import httpx
import pytest

from ssdu.harvest import METADATA_HOSTS, MetadataClient
from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter

URL = "https://api.openalex.org/works"


def http(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        allowed_hosts=METADATA_HOSTS,
        rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
    )


def test_run_request_budget_counts_failed_attempts_and_cached_reads_still_work(tmp_path):
    with http(tmp_path, lambda _: httpx.Response(200, json={"results": []})) as client:
        capture = MetadataClient(tmp_path, client=client, max_requests=1)
        first = capture.get(URL)
        with pytest.raises(RetrievalError, match="budget exhausted"):
            capture.get(URL, params={"cursor": "next"})
        assert capture.get(URL).body == first.body
        assert capture.attempts == 1 and capture.cache_hits == 1
    with http(tmp_path, lambda _: httpx.Response(503)) as client:
        capture = MetadataClient(tmp_path, client=client, max_requests=1)
        with pytest.raises(RetrievalError):
            capture.get(URL, params={"cursor": "failed"})
        assert capture.attempts == 1


def test_entire_run_byte_budget_and_retained_history(tmp_path):
    with http(tmp_path, lambda _: httpx.Response(200, content=b"12345")) as client:
        capture = MetadataClient(tmp_path, client=client, max_total_bytes=7)
        assert capture.get(URL).body == b"12345"
        with pytest.raises(RetrievalError, match="download budget"):
            capture.get(URL, params={"cursor": "next"})
        assert capture.total_bytes == 10
        assert capture.get(URL, offline=True).body == b"12345"


def test_observed_quota_stops_new_requests_but_allows_cached_pages(tmp_path):
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999"}
    with http(tmp_path, lambda _: httpx.Response(200, headers=headers, json={})) as client:
        capture = MetadataClient(tmp_path, client=client)
        first = capture.get(URL)
        assert first.metadata["headers"]["x-ratelimit-remaining"] == "0"
        with pytest.raises(RetrievalError, match="daily quota exhausted"):
            capture.get(URL, params={"cursor": "next"})
        assert capture.get(URL).cache_hit and capture.attempts == 1


def test_cache_allowlists_are_scoped_without_changing_provider_defaults(tmp_path):
    with http(tmp_path, lambda _: httpx.Response(200, json={})) as client:
        capture = MetadataClient(tmp_path, client=client)
        with pytest.raises(RetrievalError, match="allowlist"):
            capture.get("https://example.org/")
        assert capture.attempts == 0


def test_embedded_url_query_is_preserved_and_has_distinct_verified_cache_identity(tmp_path):
    seen = []

    def response(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"article": request.url.params.get("id")})

    with http(tmp_path, response) as client:
        capture = MetadataClient(tmp_path, client=client)
        first = capture.get(URL + "?id=first")
        second = capture.get(URL + "?id=second")
        assert seen == [{"id": "first"}, {"id": "second"}]
        assert first.body != second.body
        assert first.metadata["request"]["params"] == {"id": "first"}
        assert capture.get(URL + "?id=first", offline=True).body == first.body
        assert capture.attempts == 2
        with pytest.raises(ValueError, match="Duplicate URL query"):
            capture.get(URL + "?id=first&id=second")
        with pytest.raises(ValueError, match="credentials"):
            capture.get(URL + "?api_key=credential")
