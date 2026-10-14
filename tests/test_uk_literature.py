import copy
import json

import httpx
import pytest

from ssdu.harvest import METADATA_HOSTS, MetadataClient
from ssdu.http import HttpClient
from ssdu.literature_store import LiteratureStore
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter
from ssdu.uk_literature import default_plan, harvest_uk_literature


def plan(**changes):
    result = default_plan()
    result["queries"] = result["queries"][:1]
    result["page_size"] = 2
    result.update(changes)
    return result


def work(number=1, *, title="Nomis labour market research", doi=None, abstract=None):
    return {
        "id": f"https://openalex.org/W{number}",
        "doi": doi,
        "title": title,
        "publication_date": "2023-02-01",
        "abstract_inverted_index": abstract,
    }


def page(records, *, cursor=None, count=None):
    return {
        "results": records,
        "meta": {"count": len(records) if count is None else count, "next_cursor": cursor},
    }


def transport(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        allowed_hosts=METADATA_HOSTS,
        rate_limiter=RateLimiter(tmp_path / "limits.db", interval_seconds=0),
    )


def capture(tmp_path, response, specification=None):
    with transport(tmp_path, lambda _: httpx.Response(200, json=response)) as http:
        result = harvest_uk_literature(
            tmp_path, specification or plan(), client=MetadataClient(tmp_path, client=http)
        )
    return result, LiteratureStore(tmp_path).read(result["snapshot_id"])


def test_default_unique_cap_covers_all_six_bounded_searches():
    specification = default_plan()
    assert len(specification["queries"]) == 6
    assert specification["max_unique_papers"] == 2000
    assert specification["max_unique_papers"] >= (
        len(specification["queries"])
        * specification["max_pages_per_query"]
        * specification["page_size"]
    )


def test_interrupted_cursor_harvest_resumes_from_verified_page_and_replays_offline(
    tmp_path, monkeypatch
):
    calls = []
    fail = True

    def handler(request):
        cursor = request.url.params["cursor"]
        calls.append(cursor)
        if cursor == "*":
            return httpx.Response(200, json=page([work(1)], cursor="next", count=2))
        if fail:
            return httpx.Response(401)
        return httpx.Response(200, json=page([work(2)], count=2))

    with transport(tmp_path, handler) as http:
        client = MetadataClient(tmp_path, client=http)
        with pytest.raises(RetrievalError, match="Provider request failed"):
            harvest_uk_literature(tmp_path, plan(), client=client)
        assert calls == ["*", "next"]
        assert LiteratureStore(tmp_path).list() == []
        fail = False
        resumed = harvest_uk_literature(
            tmp_path, plan(), client=MetadataClient(tmp_path, client=http)
        )
        assert calls == ["*", "next", "next"]
        assert resumed["cache_hits"] == 1 and resumed["http_attempts"] == 1
    monkeypatch.setattr(
        HttpClient, "request", lambda *args, **kwargs: pytest.fail("Unexpected HTTP")
    )
    replay = harvest_uk_literature(tmp_path, plan(), offline=True)
    cached = harvest_uk_literature(tmp_path, plan())
    assert replay["snapshot_id"] == cached["snapshot_id"] == resumed["snapshot_id"]
    assert replay["replayed_snapshot"] and replay["cache_hits"] == replay["http_attempts"] == 0
    assert cached["cache_hits"] == 2 and cached["http_attempts"] == 0
    report = replay["coverage"]["queries"][0]
    assert report["exhausted"] and report["pages"] == 2 and report["unique_papers"] == 2


def test_search_only_matches_remain_unlinked_and_literal_spans_are_exact(tmp_path):
    records = [
        work(1, title="An economist studies labour"),
        work(
            2, title="Employment research", abstract={"Use": [0], "NOMIS": [1], "statistics.": [2]}
        ),
    ]
    result, document = capture(tmp_path, page(records))
    assert result["counts"]["papers"] == 2 and result["counts"]["relationships"] == 1
    papers = {paper["id"]: paper for paper in document["papers"]}
    assert papers["openalex:W1"]["candidate_status"] == "search_hit_unlinked"
    assert papers["openalex:W1"]["abstract"] is None
    assert papers["openalex:W2"]["abstract"] == "Use NOMIS statistics."
    relation = document["relationships"][0]
    assert relation["relationship_type"] == "inferred_mention"
    assert relation["review"]["status"] == "pending"
    assert relation["dataset_version"] is None
    evidence = relation["evidence"]["literal_matches"][0]
    assert evidence["field"] == "abstract" and evidence["span"] == {
        "start": 4,
        "end": 9,
        "unit": "Unicode code points",
    }
    assert papers["openalex:W2"]["abstract"][4:9] == evidence["matched_text"]
    assert evidence["location"] == "/results/1/abstract_inverted_index"
    assert result["coverage"]["unlinked_candidates"] == 1
    family = document["datasets"][0]
    assert family["coverage"] == {} and family["version"] is None and family["native_id"] is None
    assert family["retrieval_supported"] is False


def test_duplicate_ids_and_dois_preserve_every_query_page_reference(tmp_path):
    specification = plan()
    second = copy.deepcopy(specification["queries"][0])
    second.update(id="nomis_other", text='"Nomis" labour')
    specification["queries"].append(second)
    responses = [
        page([work(2, doi="https://doi.org/10.1234/Same")]),
        page([work(1, doi="10.1234/same"), work(2, doi="10.1234/same")]),
    ]
    with transport(tmp_path, lambda _: httpx.Response(200, json=responses.pop(0))) as http:
        result = harvest_uk_literature(
            tmp_path, specification, client=MetadataClient(tmp_path, client=http)
        )
    document = LiteratureStore(tmp_path).read(result["snapshot_id"])
    assert len(document["papers"]) == len(document["relationships"]) == 1
    paper = document["papers"][0]
    assert paper["id"] == "openalex:W1"
    assert paper["openalex_ids"] == ["https://openalex.org/W1", "https://openalex.org/W2"]
    assert paper["doi"] == "10.1234/same"
    assert len(paper["query_hits"]) == 3
    assert {hit["query_id"] for hit in paper["query_hits"]} == {"nomis", "nomis_other"}
    assert (
        len(
            [
                reference
                for reference in paper["references"]
                if reference["type"] == "bibliographic_search_metadata"
            ]
        )
        == 2
    )
    assert all(row["unique_papers"] == 1 for row in result["coverage"]["queries"])
    assert result["coverage"]["queries"][1]["duplicate_record_hits"] == 1
    assert document["relationships"][0]["paper_id"] == paper["id"]


def test_missing_fields_stay_null_and_bad_abstract_retains_issue_and_raw_page(tmp_path):
    malformed = work(1, abstract={"Broken": [4]}, doi="NOT A DOI")
    malformed["publication_date"] = "2023-99-45"
    result, document = capture(tmp_path, page([malformed]))
    paper = document["papers"][0]
    assert paper["abstract"] is paper["doi"] is paper["published_at"] is None
    assert {issue["field"] for issue in paper["metadata_issues"]} == {
        "abstract",
        "doi",
        "published_at",
    }
    assert all(issue["references"] for issue in paper["metadata_issues"])
    store = LiteratureStore(tmp_path)
    raw = json.loads(
        (
            store.path(result["snapshot_id"]) / "resources/openalex-search/nomis/page-0001.json"
        ).read_bytes()
    )
    assert raw["results"][0] == malformed


def test_malformed_work_is_quarantined_with_reference_instead_of_dropping_row(tmp_path):
    result, document = capture(
        tmp_path, page([{"id": "https://openalex.org/W1", "title": None}, "invalid"])
    )
    assert document["papers"] == document["relationships"] == []
    assert result["coverage"]["quarantined_records"] == 2
    assert result["coverage"]["queries"][0]["rejected_records"] == 2
    assert all(
        row["references"] and row["location"].startswith("/results/")
        for row in document["quarantine"]
    )


def test_paper_budget_tracks_rejected_page_rows_and_stops_further_requests(tmp_path):
    calls = []
    response = page([work(1), work(2)], cursor="next", count=100)
    with transport(
        tmp_path, lambda request: calls.append(request) or httpx.Response(200, json=response)
    ) as http:
        result = harvest_uk_literature(
            tmp_path, plan(max_unique_papers=1), client=MetadataClient(tmp_path, client=http)
        )
    assert len(calls) == 1 and result["counts"]["papers"] == 1
    assert result["quarantine"][0]["reason"] == "unique_paper_budget"
    row = result["coverage"]["queries"][0]
    assert row["budget_truncated"] and row["stopping_reason"] == "unique_paper_budget"
    assert row["paper_budget_rejections"] == 1 and not row["exhausted"]


def test_request_budget_scope_is_reproduced_offline(tmp_path, monkeypatch):
    specification = plan(max_requests=1)
    response = page([work(1)], cursor="next", count=50)
    result, _ = capture(tmp_path, response, specification)
    monkeypatch.setattr(
        HttpClient, "request", lambda *args, **kwargs: pytest.fail("Unexpected HTTP")
    )
    replay = harvest_uk_literature(tmp_path, specification, offline=True)
    assert replay["snapshot_id"] == result["snapshot_id"]
    row = replay["coverage"]["queries"][0]
    assert row["stopping_reason"] == "request_budget" and row["budget_truncated"]


def test_page_cap_is_reported_without_claiming_source_exhaustion(tmp_path):
    result, _ = capture(
        tmp_path, page([work()], cursor="next", count=100), plan(max_pages_per_query=1)
    )
    row = result["coverage"]["queries"][0]
    assert row["stopping_reason"] == "page_budget"
    assert row["reported_counts"] == [100] and row["budget_truncated"] and not row["exhausted"]


@pytest.mark.parametrize("cursor", ["*", "next"])
def test_cursor_loops_abort_publication_and_keep_checkpoints(tmp_path, cursor):
    calls = []

    def handler(request):
        calls.append(request.url.params["cursor"])
        next_cursor = cursor if len(calls) == 1 else "next"
        return httpx.Response(200, json=page([work()], cursor=next_cursor))

    with (
        transport(tmp_path, handler) as http,
        pytest.raises(RetrievalError, match="cursor loop"),
    ):
        harvest_uk_literature(tmp_path, plan(), client=MetadataClient(tmp_path, client=http))
    assert LiteratureStore(tmp_path).list() == []
    assert list((tmp_path / "harvest/http/captures").glob("*.body"))


@pytest.mark.parametrize(
    "response",
    [
        {"results": [], "meta": {}},
        {"results": {}, "meta": {"count": 1, "next_cursor": None}},
        {"results": [], "meta": {"count": True, "next_cursor": None}},
        {"results": [], "meta": {"count": 0, "next_cursor": 1}},
        {"results": [work(1), work(2), work(3)], "meta": {"count": 3, "next_cursor": None}},
    ],
)
def test_invalid_page_shape_aborts_snapshot(tmp_path, response):
    with (
        transport(tmp_path, lambda _: httpx.Response(200, json=response)) as http,
        pytest.raises(RetrievalError, match="OpenAlex search"),
    ):
        harvest_uk_literature(tmp_path, plan(), client=MetadataClient(tmp_path, client=http))
    assert LiteratureStore(tmp_path).list() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"page_size": 101},
        {"page_size": True},
        {"max_pages_per_query": 0},
        {"max_pages_per_query": 21},
        {"max_unique_papers": 0},
        {"max_requests": 301},
        {"max_total_bytes": 0},
        {"queries": []},
        {"queries": "Nomis"},
    ],
)
def test_invalid_bounds_fail_before_network_or_store_publication(tmp_path, changes):
    with (
        transport(tmp_path, lambda _: pytest.fail("Unexpected HTTP")) as http,
        pytest.raises(ValueError),
    ):
        harvest_uk_literature(
            tmp_path, plan(**changes), client=MetadataClient(tmp_path, client=http)
        )
    assert LiteratureStore(tmp_path).list() == []


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../bad"},
        {"aliases": []},
        {"aliases": ["Nomis", "NOMIS"]},
        {"source_url": "https://secret@nomis.org"},
    ],
)
def test_invalid_query_fields_fail_before_network(tmp_path, change):
    specification = plan()
    specification["queries"][0].update(change)
    with (
        transport(tmp_path, lambda _: pytest.fail("Unexpected HTTP")) as http,
        pytest.raises(ValueError),
    ):
        harvest_uk_literature(tmp_path, specification, client=MetadataClient(tmp_path, client=http))


def test_duplicate_query_ids_and_conflicting_family_metadata_are_rejected(tmp_path):
    specification = plan()
    duplicate = copy.deepcopy(specification["queries"][0])
    specification["queries"].append(duplicate)
    with pytest.raises(ValueError, match="Duplicate query"):
        harvest_uk_literature(tmp_path, specification, offline=True)
    duplicate["id"] = "other"
    duplicate["family_title"] = "A conflicting family label"
    with pytest.raises(ValueError, match="shared dataset family"):
        harvest_uk_literature(tmp_path, specification, offline=True)


def test_explicit_refresh_preserves_previous_capture_and_snapshot(tmp_path):
    responses = [page([work(1)]), page([work(2)])]
    with transport(tmp_path, lambda _: httpx.Response(200, json=responses.pop(0))) as http:
        client = MetadataClient(tmp_path, client=http)
        old = harvest_uk_literature(tmp_path, plan(), client=client)
        new = harvest_uk_literature(tmp_path, plan(), client=client, refresh=True)
    assert old["snapshot_id"] != new["snapshot_id"]
    assert len(LiteratureStore(tmp_path).list()) == 2
    assert len(list((tmp_path / "harvest/http/captures").glob("*.body"))) == 2
    assert (
        harvest_uk_literature(tmp_path, plan(), offline=True)["snapshot_id"] == new["snapshot_id"]
    )


def test_offline_cache_miss_fails_without_network_and_refresh_conflict_is_preflight(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        HttpClient, "request", lambda *args, **kwargs: pytest.fail("Unexpected HTTP")
    )
    with pytest.raises(RetrievalError, match="Offline literature cache miss"):
        harvest_uk_literature(tmp_path, plan(), offline=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        harvest_uk_literature(tmp_path, plan(), offline=True, refresh=True)


def test_fresh_quota_exhaustion_stops_uncached_pages_and_offline_replays_exact_scope(
    tmp_path, monkeypatch
):
    calls = []

    def handler(request):
        calls.append(request.url.params["cursor"])
        if request.url.params["cursor"] == "*":
            return httpx.Response(
                200,
                json=page([work(1)], cursor="next", count=2),
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1234"},
            )
        return httpx.Response(
            200, json=page([work(2)], count=2), headers={"X-RateLimit-Remaining": "5"}
        )

    with transport(tmp_path, handler) as http:
        first = harvest_uk_literature(
            tmp_path, plan(), client=MetadataClient(tmp_path, client=http)
        )
        assert calls == ["*"]
        assert first["counts"]["papers"] == 1
        query = first["coverage"]["queries"][0]
        assert query["stopping_reason"] == "quota_exhausted" and query["budget_truncated"]
        observation = query["quota_observations"][0]
        assert observation["headers"]["x-ratelimit-remaining"] == "0"
        assert observation["references"] and not query["exhausted"]
    with monkeypatch.context() as blocked:
        blocked.setattr(
            HttpClient, "request", lambda *args, **kwargs: pytest.fail("Unexpected HTTP")
        )
        replay = harvest_uk_literature(tmp_path, plan(), offline=True)
        assert replay["snapshot_id"] == first["snapshot_id"]
        assert replay["replayed_snapshot"] and replay["http_attempts"] == 0
        assert replay["coverage"] == first["coverage"]
    with transport(tmp_path, handler) as http:
        # A cached quota header records an old observation, so a new run may resume.
        resumed = harvest_uk_literature(
            tmp_path, plan(), client=MetadataClient(tmp_path, client=http)
        )
    assert calls == ["*", "next"]
    assert resumed["counts"]["papers"] == 2 and resumed["cache_hits"] == 1
    assert resumed["snapshot_id"] != first["snapshot_id"]
    assert resumed["coverage"]["queries"][0]["exhausted"]


def test_quota_zero_can_continue_cached_pages_without_another_http_attempt(tmp_path):
    first_page = page([work(1)], cursor="next", count=2)

    def handler(request):
        if request.url.params["cursor"] == "*":
            return httpx.Response(200, json=first_page)
        return httpx.Response(200, json=page([work(2)], count=2))

    with transport(tmp_path, handler) as http:
        original = harvest_uk_literature(
            tmp_path, plan(), client=MetadataClient(tmp_path, client=http)
        )
    calls = []
    with transport(
        tmp_path,
        lambda request: (
            calls.append(request)
            or httpx.Response(200, json=first_page, headers={"X-RateLimit-Remaining": "0"})
        ),
    ) as http:
        refreshed = harvest_uk_literature(
            tmp_path, plan(), refresh=True, client=MetadataClient(tmp_path, client=http)
        )
    assert len(calls) == 1 and refreshed["cache_hits"] == 1
    assert refreshed["counts"]["papers"] == original["counts"]["papers"] == 2
    assert refreshed["coverage"]["queries"][0]["exhausted"]


@pytest.mark.parametrize("bad", [b"not JSON", b"[]", b'{"schema_version":1}', b"x" * 8193])
def test_malformed_completion_pointer_fails_closed(tmp_path, bad):
    result, _ = capture(tmp_path, page([work()]))
    pointer = tmp_path / "harvest/uk-literature" / f"{result['plan_sha256']}.json"
    pointer.write_bytes(bad)
    with pytest.raises(RetrievalError, match="completion pointer"):
        harvest_uk_literature(tmp_path, plan(), offline=True)


def test_completion_pointer_cannot_rebind_a_different_plan_or_corrupt_snapshot(tmp_path):
    first, _ = capture(tmp_path, page([work()]))
    second_plan = plan(max_pages_per_query=1)
    with transport(tmp_path, lambda _: pytest.fail("Unexpected HTTP")) as http:
        second = harvest_uk_literature(
            tmp_path, second_plan, client=MetadataClient(tmp_path, client=http)
        )
    pointer = tmp_path / "harvest/uk-literature" / f"{first['plan_sha256']}.json"
    pointer.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_sha256": first["plan_sha256"],
                "snapshot_id": second["snapshot_id"],
            }
        )
    )
    with pytest.raises(RetrievalError, match="completion pointer"):
        harvest_uk_literature(tmp_path, plan(), offline=True)
    pointer.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_sha256": first["plan_sha256"],
                "snapshot_id": first["snapshot_id"],
            }
        )
    )
    store = LiteratureStore(tmp_path)
    (
        store.path(first["snapshot_id"]) / "resources/openalex-search/nomis/page-0001.json"
    ).write_text("corrupt")
    with pytest.raises(RetrievalError, match="completion pointer"):
        harvest_uk_literature(tmp_path, plan(), offline=True)


def test_metadata_conflicts_are_attributed_and_missing_abstract_can_be_filled(tmp_path):
    specification = plan()
    second = copy.deepcopy(specification["queries"][0])
    second.update(id="nomis_second", text='"Nomis" use')
    specification["queries"].append(second)
    responses = [
        page([work(1, title="Nomis initial title")]),
        page([work(1, title="Nomis changed title", abstract={"NOMIS": [0], "evidence.": [1]})]),
    ]
    with transport(tmp_path, lambda _: httpx.Response(200, json=responses.pop(0))) as http:
        result = harvest_uk_literature(
            tmp_path, specification, client=MetadataClient(tmp_path, client=http)
        )
    paper = LiteratureStore(tmp_path).read(result["snapshot_id"])["papers"][0]
    assert paper["abstract"] == "NOMIS evidence."
    assert paper["metadata_conflicts"] and paper["metadata_conflicts"][0]["field"] == "title"
    assert paper["metadata_conflicts"][0]["references"]
    assert len(paper["query_hits"]) == 2


def test_retained_byte_budget_aborts_publication(tmp_path):
    with (
        transport(tmp_path, lambda _: httpx.Response(200, json=page([work()]))) as http,
        pytest.raises(RetrievalError, match="byte budget"),
    ):
        harvest_uk_literature(
            tmp_path, plan(max_total_bytes=1000), client=MetadataClient(tmp_path, client=http)
        )
    assert LiteratureStore(tmp_path).list() == []
    assert not list((tmp_path / "harvest/uk-literature").glob("*.json"))
