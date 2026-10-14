import json

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.literature_ingest import HOSTS, LiteratureClient, capture_literature
from ssdu.literature_sources import RICH_CONTEXT, VOCABULARY
from ssdu.literature_store import LiteratureStore
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter

URL = "https://api.openalex.org/works"


def transport(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        allowed_hosts=HOSTS,
        rate_limiter=RateLimiter(tmp_path / "limits.db", interval_seconds=0),
    )


def test_cache_and_offline_reads_never_repeat_a_request(tmp_path):
    requests = []
    with transport(
        tmp_path,
        lambda request: requests.append(request) or httpx.Response(200, json={"results": []}),
    ) as http:
        client = LiteratureClient(tmp_path, client=http)
        first = client.get(URL, params={"filter": "doi:10.1234/example"})
        second = client.get(URL, params={"filter": "doi:10.1234/example"}, offline=True)
        assert first.body == second.body and first.metadata == second.metadata
        assert second.cache_hit and len(requests) == 1
        assert client.attempts == 1 and client.cache_hits == 1


def test_missing_or_damaged_cache_fails_without_network(tmp_path):
    calls = []
    with transport(
        tmp_path, lambda request: calls.append(request) or httpx.Response(200, json={"results": []})
    ) as http:
        client = LiteratureClient(tmp_path, client=http)
        with pytest.raises(RetrievalError, match="Offline literature cache miss"):
            client.get(URL, offline=True)
        capture = client.get(URL)
        body = client.root / "captures" / f"{capture.metadata['body_sha256']}.body"
        body.write_bytes(b"corrupt")
        with pytest.raises(RetrievalError, match="Invalid retained literature response"):
            client.get(URL)
        assert len(calls) == 1


def test_refresh_retains_old_responses_and_reuses_unchanged_capture(tmp_path):
    response = {"results": []}
    with transport(tmp_path, lambda _: httpx.Response(200, json=response)) as http:
        client = LiteratureClient(tmp_path, client=http)
        first = client.get(URL)
        unchanged = client.get(URL, refresh=True)
        assert first.metadata == unchanged.metadata
        response["results"] = [{"id": "new"}]
        changed = client.get(URL, refresh=True)
        assert changed.metadata["body_sha256"] != first.metadata["body_sha256"]
        assert len(list((client.root / "captures").glob("*.body"))) == 2
        assert client.get(URL, offline=True).body == changed.body
        assert json.loads(first.body) == {"results": []}


@pytest.mark.parametrize(
    "url,params",
    [
        ("https://example.org/private", None),
        ("https://user:secret@api.openalex.org/works", None),
        (URL + "?api_key=secret", None),
        (URL, {"api_key": "secret"}),
        (URL, {"filter": ["bad"]}),
    ],
)
def test_source_and_parameter_bounds_fail_before_request(tmp_path, url, params):
    with (
        transport(tmp_path, lambda _: pytest.fail("Unexpected HTTP")) as http,
        pytest.raises((ValueError, RetrievalError)),
    ):
        LiteratureClient(tmp_path, client=http).get(url, params=params)


def test_existing_provider_clients_keep_their_original_allowlist(tmp_path):
    with (
        HttpClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP")),
            rate_limiter=RateLimiter(tmp_path / "limits.db", interval_seconds=0),
        ) as http,
        pytest.raises(RetrievalError, match="allowlist"),
    ):
        http.get(URL)


def tiny_graph():
    return {
        "@context": RICH_CONTEXT,
        "@graph": [
            {
                "@id": VOCABULARY + "provider-1",
                "@type": "Provider",
                "dct:title": {"@value": "Survey provider"},
            },
            {
                "@id": VOCABULARY + "dataset-1",
                "@type": "Dataset",
                "dct:title": {"@value": "Household survey"},
                "dct:publisher": {"@value": "provider-1"},
            },
            {
                "@id": VOCABULARY + "publication-1",
                "@type": "ResearchPublication",
                "dct:title": {"@value": "Household inequality"},
                "dct:identifier": {"@value": "https://doi.org/10.1234/example"},
                "cito:citesAsDataSource": {"@id": VOCABULARY + "dataset-1"},
            },
        ],
    }


def test_interrupted_pipeline_resumes_and_replays_without_http(tmp_path, monkeypatch):
    requests = []
    failing = True

    def handler(request):
        requests.append(str(request.url))
        if request.url.path.endswith("corpus.jsonld"):
            return httpx.Response(200, json=tiny_graph())
        if request.url.path.endswith("LICENSE"):
            return httpx.Response(200, text="CC0 1.0 Universal")
        if failing:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.1234/example",
                        "title": "Household inequality",
                        "publication_date": "2020-01-01",
                        "abstract_inverted_index": {"Household": [0], "outcomes.": [1]},
                    }
                ]
            },
        )

    plan = {"schema_version": 1, "rich_context": {"max_papers": 1}, "openalex": {"max_papers": 1}}
    with transport(tmp_path, handler) as http:
        first = LiteratureClient(tmp_path, client=http)
        with pytest.raises(RetrievalError, match="Provider request failed"):
            capture_literature(tmp_path, plan, client=first)
        assert first.attempts == 3
        assert LiteratureStore(tmp_path).list() == []
        failing = False
        resumed = LiteratureClient(tmp_path, client=http)
        captured = capture_literature(tmp_path, plan, client=resumed)
        assert captured["provider_attempts"] == 1 and captured["cache_hits"] == 2
        assert len(requests) == 4
        monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Unexpected HTTP"))
        offline = capture_literature(tmp_path, plan, offline=True)
        assert offline["snapshot_id"] == captured["snapshot_id"]
        assert offline["provider_attempts"] == 0 and offline["cache_hits"] == 3
    document = LiteratureStore(tmp_path).read(captured["snapshot_id"])
    assert document["papers"][0]["abstract"] == "Household outcomes."
    assert document["relationships"][0]["relationship_type"] == "source_asserted_use"
    assert document["datasets"][0]["version"] is None
    assert document["papers"][0]["references"] and document["relationships"][0]["references"]


@pytest.mark.parametrize(
    "plan",
    [
        {"schema_version": True},
        {"schema_version": 1, "rich_context": []},
        {"schema_version": 1, "openalex": []},
        {"schema_version": 1, "rich_context": {"revision": "master"}},
        {"schema_version": 1, "rich_context": {"max_papers": 0}},
        {"schema_version": 1, "rich_context": {"max_papers": True}},
        {"schema_version": 1, "openalex": {"max_papers": 101}},
        {"schema_version": 1, "rich_context": {"publication_ids": "publication-1"}},
        {"schema_version": 1, "rich_context": {"max_papers": 1, "publication_ids": ["a", "b"]}},
    ],
)
def test_plan_preflight_rejects_invalid_budgets_without_http(tmp_path, monkeypatch, plan):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    with pytest.raises((TypeError, ValueError)):
        capture_literature(tmp_path, plan)


@pytest.mark.parametrize("damage", ["pointer-list", "pointer-id", "metadata-list"])
def test_malformed_retained_index_fails_closed(tmp_path, damage):
    with transport(tmp_path, lambda _: httpx.Response(200, json={"results": []})) as http:
        client = LiteratureClient(tmp_path, client=http)
        capture = client.get(URL)
        pointer = next((client.root / "requests").glob("*/current.json"))
        if damage == "pointer-list":
            pointer.write_text("[]")
        elif damage == "pointer-id":
            pointer.write_text('{"capture_id": "../escape"}')
        else:
            (client.root / "captures" / f"{capture.metadata['capture_id']}.json").write_text("[]")
        with pytest.raises(
            RetrievalError,
            match="Invalid (retained literature response|literature capture metadata)",
        ):
            client.get(URL)
        assert client.attempts == 1
