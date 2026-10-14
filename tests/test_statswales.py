import copy
import json

import httpx
import pandas as pd
import pytest

from ssdu.adapters import statswales
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError

DATASET = statswales.DEFAULT_ID
FILTER_ID = "abcdefgh1234"
REVISION = "revision-1"


@pytest.fixture
def source_fixture():
    return {
        "metadata": {
            "id": DATASET,
            "published_revision": {
                "id": REVISION,
                "revision_index": 2,
                "publish_at": "2026-05-28T16:05:00Z",
                "metadata": [{"language": "en-GB", "title": "Example poverty data"}],
            },
        },
        "dimensions": [
            {
                "columnName": "Area",
                "factTableColumn": "AreaID",
                "values": [
                    {
                        "reference": "001",
                        "description": "Wales",
                        "children": [
                            {"reference": "002", "description": "Cardiff"},
                            {"reference": "003", "description": "Swansea"},
                        ],
                    },
                ],
            },
        ],
        "rows": [
            {"Area": "Wales", "Data values": "          22", "Notes": None, "Area_sort": 1},
            {"Area": "Cardiff", "Data values": "*", "Notes": "suppressed", "Area_sort": 2},
            {"Area": "Swansea", "Data values": "001", "Notes": "p", "Area_sort": 3},
        ],
    }


def client_for(source, *, total=3, pages=None, changed=False, changed_count=False):
    calls = []
    metadata_reads = 0

    def handler(request):
        nonlocal metadata_reads
        calls.append(request)
        path = request.url.path
        if request.method == "POST":
            assert path == f"/v2/{DATASET}/data"
            assert json.loads(request.content) == {
                "filters": [{"AreaID": ["001", "002", "003"]}],
                "options": {"data_value_type": "formatted"},
            }
            return httpx.Response(200, json={"filterId": FILTER_ID})
        if path == f"/v2/{DATASET}/filters":
            return httpx.Response(200, json=source["dimensions"])
        if path == f"/v2/{DATASET}/query/{FILTER_ID}":
            info = {"id": FILTER_ID, "datasetId": DATASET, "revisionId": REVISION}
            if total is not None:
                info["totalLines"] = total + int(changed_count and metadata_reads == 2)
            return httpx.Response(200, json=info)
        if path == f"/v2/{DATASET}":
            metadata_reads += 1
            metadata = copy.deepcopy(source["metadata"])
            if changed and metadata_reads == 2:
                metadata["published_revision"]["id"] = "revision-2"
            return httpx.Response(200, json=metadata)
        if path == f"/v2/{DATASET}/data/{FILTER_ID}":
            assert request.url.params["format"] == "json"
            page = int(request.url.params["page_number"])
            size = int(request.url.params["page_size"])
            rows = (
                pages.get(page, [])
                if pages is not None
                else source["rows"][(page - 1) * size : page * size]
            )
            return httpx.Response(200, json=rows)
        raise AssertionError(f"Unexpected request: {request}")

    return HttpClient(transport=httpx.MockTransport(handler), retries=0), calls


def request_for(**kwargs):
    return Request(
        "statswales",
        DATASET,
        {"filters": [{"Area": ["001", "002", "003"]}]},
        page_size=2,
        max_rows=kwargs.get("max_rows", 10),
    )


def test_complete_pages_preserve_values_notes_and_codes(source_fixture):
    client, calls = client_for(source_fixture)
    with client:
        acquired = statswales.fetch(request_for(), client)
    assert acquired.frame["Data values"].tolist() == ["          22", "*", "001"]
    assert acquired.frame["Notes"].tolist() == [None, "suppressed", "p"]
    assert acquired.frame["Area__code"].tolist() == ["001", "002", "003"]
    assert acquired.frame["Area_sort"].tolist() == [1, 2, 3]
    pages = [int(call.url.params["page_number"]) for call in calls if "/data/" in call.url.path]
    assert pages == [1, 2]  # Count is independently rechecked; an out-of-range page gives HTTP 400.
    assert acquired.metadata["provider_row_count"] == 3
    assert acquired.metadata["dimensions"] == source_fixture["dimensions"]
    assert acquired.release["revision_stable_during_retrieval"] is True
    assert acquired.release["requested_revision_pinned"] is False
    assert len(client.resources) == 8


def test_truncated_page_rejected_against_provider_count(source_fixture):
    client, _ = client_for(source_fixture, pages={1: source_fixture["rows"][:1]})
    with client, pytest.raises(RetrievalError, match="page length disagrees"):
        statswales.fetch(request_for(), client)


def test_max_rows_rejected_before_data_download(source_fixture):
    client, calls = client_for(source_fixture, total=11)
    with client, pytest.raises(RetrievalError, match="max_rows=10"):
        statswales.fetch(request_for(), client)
    assert not any("/data/" in call.url.path for call in calls)


def test_missing_count_proves_terminal_page_and_reports_limit(source_fixture):
    client, _ = client_for(source_fixture, total=None)
    with client:
        acquired = statswales.fetch(request_for(), client)
    assert len(acquired.frame) == 3
    assert acquired.metadata["provider_row_count"] is None
    assert "no provider count" in acquired.metadata["completeness_method"]


def test_missing_count_cannot_silently_truncate_at_max_rows(source_fixture):
    client, _ = client_for(source_fixture, total=None)
    with client, pytest.raises(RetrievalError, match="exceeds max_rows=2"):
        statswales.fetch(request_for(max_rows=2), client)


def test_repeated_page_rejected_without_provider_count(source_fixture):
    first = source_fixture["rows"][:2]
    client, _ = client_for(source_fixture, total=None, pages={1: first, 2: first})
    with client, pytest.raises(RetrievalError, match="pagination did not progress"):
        statswales.fetch(request_for(), client)


def test_revision_change_rejected(source_fixture):
    client, _ = client_for(source_fixture, changed=True)
    with client, pytest.raises(RetrievalError, match="revision changed"):
        statswales.fetch(request_for(), client)


def test_count_change_during_retrieval_rejected(source_fixture):
    client, _ = client_for(source_fixture, changed_count=True)
    with client, pytest.raises(RetrievalError, match="count or revision changed"):
        statswales.fetch(request_for(), client)


def test_ambiguous_code_is_not_inferred(source_fixture):
    source_fixture["dimensions"][0]["values"].append({"reference": "004", "description": "Cardiff"})
    client, _ = client_for(source_fixture)
    with client:
        acquired = statswales.fetch(request_for(), client)
    assert pd.isna(acquired.frame.loc[1, "Area__code"])
    assert acquired.frame.loc[1, "Area"] == "Cardiff"
    assert acquired.metadata["code_recovery"]["Area"]["ambiguous_labels"] == ["Cardiff"]


def test_unknown_selector_fails_before_query_creation(source_fixture):
    client, calls = client_for(source_fixture)
    request = Request("statswales", DATASET, {"filters": [{"invented": ["001"]}]})
    with client, pytest.raises(RetrievalError, match="Unknown or ambiguous"):
        statswales.fetch(request, client)
    assert all(call.method == "GET" for call in calls)


def test_search_uses_bounded_public_catalogue():
    def handler(request):
        assert request.url.path == "/v2/search"
        assert request.url.params["keywords"] == "poverty"
        assert request.url.params["page_size"] == "25"
        return httpx.Response(
            200, json={"data": [{"id": DATASET, "title": "Poverty"}], "count": 33}
        )

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        assert statswales.search("poverty", client) == [
            {"id": DATASET, "title": "Poverty", "provider": "statswales"}
        ]
