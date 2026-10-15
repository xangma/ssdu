"""Invented provider responses: browse and inspect unrelated datasets before fetching.

Protocol references (no provider observations or metadata captures are included):
https://www.nomisweb.co.uk/api/v01/help
https://api.stats.gov.wales/v2/docs
"""

import copy
import json
from contextlib import contextmanager

import httpx
import pandas as pd
import pytest

from ssdu.adapters import nomis, statswales
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.normalization import statswales as normalize_wales

WALES_IDS = [f"00000000-0000-4000-8000-{number:012d}" for number in range(1, 6)]


def nomis_definition(identity="NM_999_2"):
    return {
        "structure": {
            "keyfamilies": {
                "keyfamily": [
                    {
                        "id": identity,
                        "name": {"value": "Invented industry table"},
                        "components": {
                            "dimension": [{"conceptref": "INDUSTRY", "codelist": "CL_INDUSTRY"}]
                        },
                    }
                ]
            }
        }
    }


def wales_metadata(identity):
    return {
        "id": identity,
        "published_revision": {
            "id": "invented-release-2",
            "metadata": [{"language": "en-GB", "title": "Invented transport table"}],
        },
    }


def wales_dimensions():
    return [
        {
            "factTableColumn": "ModeKey",
            "columnName": "Travel mode",
            "values": [
                {
                    "reference": "00",
                    "description": "All invented modes",
                    "children": [
                        {"reference": "01", "description": "Walking"},
                        {"reference": "02", "description": "Cycling"},
                    ],
                }
            ],
        }
    ]


def test_nomis_catalogue_exposes_continuation_without_observations():
    document = nomis_definition()
    document["structure"]["keyfamilies"]["keyfamily"].append(
        nomis_definition("NM_777_9")["structure"]["keyfamilies"]["keyfamily"][0]
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/dataset/def.sdmx.json")
        return httpx.Response(200, json=document)

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        first = nomis.list_datasets("", client, limit=1)
        second = nomis.list_datasets("", client, limit=1, offset=first["next_offset"])
    assert first["total"] == 2 and first["next_offset"] == 1
    assert second["items"][0]["id"] == "NM_777_9"
    assert second["next_offset"] is None
    assert len(calls) == 2


def test_nomis_description_and_codelists_precede_data():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/industry.def.sdmx.json"):
            return httpx.Response(
                200,
                json={
                    "structure": {
                        "codelists": {
                            "codelist": {
                                "id": "CL_INDUSTRY",
                                "code": [
                                    {"value": "001", "description": {"value": "Invented services"}},
                                    {
                                        "value": "002",
                                        "description": {"value": "Invented manufacturing"},
                                    },
                                ],
                            }
                        }
                    }
                },
            )
        assert request.url.path.endswith("/NM_999_2/def.sdmx.json")
        return httpx.Response(200, json=nomis_definition())

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        descriptor = nomis.describe_dataset("NM_999_2", client)
        codes = nomis.list_dimension_values("NM_999_2", "industry", client, limit=1, offset=1)
    assert descriptor["dimensions"][0]["id"] == "industry"
    assert descriptor["capabilities"]["server_side_selection"]
    assert descriptor["references"][0]["url"].endswith("/help")
    assert codes["items"][0]["value"] == "002" and codes["total"] == 2
    assert codes["scope"] == "provider" and codes["next_offset"] is None
    assert all(".data." not in str(call.url) for call in calls)


def test_nomis_non_geographic_non_temporal_dataset_fetches_with_bounds():
    def handler(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=nomis_definition())
        assert request.url.params["industry"] == "001"
        assert request.url.params["recordlimit"] == "2"
        assert request.url.params["setallwherenotspecified"] == "false"
        return httpx.Response(
            200, text=("INDUSTRY,OBS_VALUE,OBS_STATUS,RECORD_COUNT,RECORD_OFFSET\n001,7,A,1,0\n")
        )

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        acquired = nomis.fetch(
            Request("nomis", "NM_999_2", {"industry": "001"}, page_size=2), client
        )
    assert acquired.dataset.to_arrow().to_pydict()["industry"] == ["001"]
    assert acquired.release["observed_periods"] == []


def test_nomis_metadata_identity_mismatch_rejected():
    with (
        HttpClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=nomis_definition("NM_777_9"))
            )
        ) as client,
        pytest.raises(RetrievalError, match="different dataset"),
    ):
        nomis.describe_dataset("NM_999_2", client)


@pytest.mark.parametrize("total", [True, False])
def test_statswales_catalogue_covers_arbitrary_offsets(total):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/v2/search"
        page, size = int(request.url.params["page_number"]), int(request.url.params["page_size"])
        rows = [{"id": identity, "title": "Invented table"} for identity in WALES_IDS]
        response = {"data": rows[(page - 1) * size : page * size]}
        if total:
            response["pagination"] = {"total": len(rows), "page_number": page, "page_size": size}
        return httpx.Response(200, json=response)

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        result = statswales.list_datasets("transport", client, limit=2, offset=1)
        final = statswales.list_datasets("transport", client, limit=2, offset=result["next_offset"])
    assert [row["id"] for row in result["items"]] == WALES_IDS[1:3]
    assert result["next_offset"] == 3
    assert final["next_offset"] is None
    assert [row["id"] for row in final["items"]] == WALES_IDS[3:]
    assert result["total"] == (5 if total else None)


def test_statswales_full_page_without_count_has_continuation():
    rows = [{"id": identity, "title": "Invented table"} for identity in WALES_IDS[:2]]
    with HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": rows}))
    ) as client:
        result = statswales.list_datasets("", client, limit=2)
    assert result["total"] is None and result["next_offset"] == 2


def test_statswales_description_and_nested_codes_never_download_observations():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/filters"):
            return httpx.Response(200, json=wales_dimensions())
        assert request.url.path == f"/v2/{WALES_IDS[0]}"
        return httpx.Response(200, json=wales_metadata(WALES_IDS[0]))

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        descriptor = statswales.describe_dataset(WALES_IDS[0], client)
        page = statswales.list_dimension_values(WALES_IDS[0], "ModeKey", client, limit=1, offset=1)
        matches = statswales.list_dimension_values(
            WALES_IDS[0], "Travel mode", client, search="cycl"
        )
    assert descriptor["dimensions"][0]["value_count"] == 3
    assert descriptor["release"]["revision_id"] == "invented-release-2"
    assert page["items"] == [{"value": "01", "label": "Walking"}]
    assert page["next_offset"] == 2
    assert matches["items"] == [{"value": "02", "label": "Cycling"}]
    assert len(calls) == 6


@pytest.mark.parametrize("bad", [True, -1, "two"])
def test_invalid_statswales_catalogue_counter_rejected(bad):
    with (
        HttpClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [], "count": bad})
            )
        ) as client,
        pytest.raises(RetrievalError, match="count is invalid"),
    ):
        statswales.list_datasets("", client)


def test_statswales_repeated_catalogue_page_rejected():
    batch = [{"id": identity, "title": "Invented table"} for identity in WALES_IDS[:2]]
    with (
        HttpClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": batch}))
        ) as client,
        pytest.raises(RetrievalError, match="repeats a dataset"),
    ):
        statswales.list_datasets("", client, limit=2, offset=1)


def test_statswales_filter_validates_unrelated_dataset_codes_before_post():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=(
                wales_dimensions()
                if request.url.path.endswith("/filters")
                else wales_metadata(WALES_IDS[0])
            ),
        )

    with (
        HttpClient(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RetrievalError, match="Unknown StatsWales reference code"),
    ):
        statswales.fetch(
            Request("statswales", WALES_IDS[0], {"filters": [{"ModeKey": ["bad"]}]}), client
        )
    assert len(calls) == 2


def test_statswales_unrelated_dataset_fetches_and_preserves_unknown_units():
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if request.method == "POST":
            assert json.loads(request.content)["filters"] == [{"ModeKey": ["01"]}]
            return httpx.Response(200, json={"filterId": "synthetic123"})
        if path.endswith("/filters"):
            return httpx.Response(200, json=wales_dimensions())
        if "/query/" in path:
            return httpx.Response(
                200,
                json={
                    "datasetId": WALES_IDS[0],
                    "revisionId": "invented-release-2",
                    "totalLines": 1,
                },
            )
        if "/data/" in path:
            return httpx.Response(200, json=[{"Travel mode": "Walking", "Observed amount": "007"}])
        return httpx.Response(200, json=wales_metadata(WALES_IDS[0]))

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        acquired = statswales.fetch(
            Request("statswales", WALES_IDS[0], {"filters": [{"ModeKey": ["01"]}]}, max_rows=3),
            client,
        )
    assert acquired.dataset.to_arrow().to_pydict()["value_raw"] == ["007"]
    assert acquired.dataset.to_arrow().to_pydict()["unit"] == [None]
    assert acquired.frame["Travel mode__code"].tolist() == ["01"]
    assert len(calls) == 7


def test_statswales_ambiguous_payload_columns_are_not_guessed():
    acquired = Acquisition(
        provider="statswales",
        dataset_id=WALES_IDS[0],
        title="Invented ambiguous table",
        frame=pd.DataFrame({"Travel mode": ["Walking"], "First": ["7"], "Second": ["8"]}),
        metadata={"dimensions": copy.deepcopy(wales_dimensions())},
        release={},
        licence="Synthetic fixture",
        citation="Invented; not official statistics.",
        source_url="https://example.invalid/fixture",
    )
    with pytest.raises(RetrievalError, match="ambiguous StatsWales data-value schema"):
        normalize_wales(acquired)


def test_selection_validation_uses_retained_metadata_without_io():
    nomis_descriptor = {
        "provider": "nomis",
        "dataset_id": "NM_999_2",
        "dimensions": [{"id": "industry"}],
    }
    nomis.validate_selection(Request("nomis", "NM_999_2", {"industry": "001"}), nomis_descriptor)
    with pytest.raises(ValueError, match="Unsupported Nomis selectors"):
        nomis.validate_selection(Request("nomis", "NM_999_2", {"bad": "001"}), nomis_descriptor)
    wales_descriptor = {
        "provider": "statswales",
        "dataset_id": WALES_IDS[0],
        "dimensions": [{"id": "ModeKey", "source_metadata": wales_dimensions()[0]}],
    }
    statswales.validate_selection(
        Request("statswales", WALES_IDS[0], {"filters": [{"ModeKey": ["01"]}]}), wales_descriptor
    )
    with pytest.raises(RetrievalError, match="Unknown StatsWales reference code"):
        statswales.validate_selection(
            Request("statswales", WALES_IDS[0], {"filters": [{"ModeKey": ["unlisted"]}]}),
            wales_descriptor,
        )


def test_statswales_declared_total_cannot_hide_incomplete_catalogue():
    with (
        HttpClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [], "count": 10})
            )
        ) as client,
        pytest.raises(RetrievalError, match="disagrees with its declared count"),
    ):
        statswales.list_datasets("", client, limit=2)


def test_statswales_pagination_metadata_must_match_requested_page():
    with (
        HttpClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "data": [],
                        "pagination": {"page_number": 1, "page_size": 2},
                    },
                )
            )
        ) as client,
        pytest.raises(RetrievalError, match="does not match request"),
    ):
        statswales.list_datasets("", client, limit=2, offset=2)


@pytest.mark.parametrize("provider", ["nomis", "statswales"])
@pytest.mark.parametrize("change", ["revision", "interpretation"])
def test_new_metadata_scope_cannot_reuse_old_data_responses(provider, change):
    """Identical data URLs must not leak cached cells across metadata versions."""
    state = {"value": "7", "revision": "fixture-1", "label": "Walking"}
    calls, cache = [], {}

    def handler(request):
        path = request.url.path
        if provider == "nomis":
            if path.endswith(".json"):
                document = nomis_definition()
                family = document["structure"]["keyfamilies"]["keyfamily"][0]
                family["name"]["value"] = state["label"]
                family["annotations"] = {
                    "annotation": [
                        {
                            "annotationtitle": "LastRevised",
                            "annotationtext": state["revision"],
                        }
                    ]
                }
                return httpx.Response(200, json=document)
            return httpx.Response(
                200,
                text=(
                    "INDUSTRY,OBS_VALUE,OBS_STATUS,RECORD_COUNT,RECORD_OFFSET\n"
                    f"001,{state['value']},A,1,0\n"
                ),
            )
        if request.method == "POST":
            return httpx.Response(200, json={"filterId": "synthetic123"})
        if path.endswith("/filters"):
            dimensions = wales_dimensions()
            dimensions[0]["values"][0]["children"][0]["description"] = state["label"]
            return httpx.Response(200, json=dimensions)
        if "/query/" in path:
            return httpx.Response(
                200,
                json={
                    "datasetId": WALES_IDS[0],
                    "revisionId": state["revision"],
                    "totalLines": 1,
                },
            )
        if "/data/" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "Travel mode": state["label"],
                        "Data values": state["value"],
                    }
                ],
            )
        document = wales_metadata(WALES_IDS[0])
        document["published_revision"]["id"] = state["revision"]
        return httpx.Response(200, json=document)

    class ScopedCache:
        active = None

        def __init__(self, client):
            self.client = client

        @contextmanager
        def scope(self, token):
            previous, self.active = self.active, token
            try:
                yield self
            finally:
                self.active = previous

        def request(self, method, url, **kwargs):
            calls.append((method, url, self.active))
            key = (self.active, method, url, json.dumps(kwargs, sort_keys=True))
            if self.active is None:
                return self.client.request(method, url, **kwargs)
            if key not in cache:
                cache[key] = self.client.request(method, url, **kwargs)
            return cache[key]

        def get(self, url, params=None):
            return self.request("GET", url, params=params)

        def post(self, url, json):
            return self.request("POST", url, json=json)

    request = (
        Request("nomis", "NM_999_2", {"industry": "001"})
        if provider == "nomis"
        else Request("statswales", WALES_IDS[0], {"filters": [{"ModeKey": ["01"]}]})
    )
    adapter = nomis if provider == "nomis" else statswales
    with HttpClient(transport=httpx.MockTransport(handler)) as http:
        client = ScopedCache(http)
        first = adapter.fetch(request, client)
        state["value"] = "9"
        state["revision" if change == "revision" else "label"] = "fixture-2"
        second = adapter.fetch(request, client)
    assert first.dataset.to_arrow().to_pydict()["value_raw"] == ["7"]
    assert second.dataset.to_arrow().to_pydict()["value_raw"] == ["9"]
    data_scopes = {
        scope
        for _, url, scope in calls
        if ".data.csv" in url or url.endswith("/data") or "/data/" in url
    }
    assert None not in data_scopes and len(data_scopes) == 2
    assert all(
        scope is None for _, url, scope in calls if "/data" not in url and ".data.csv" not in url
    )
