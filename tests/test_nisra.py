"""Invented JSON-stat cubes; no third-party dataset responses are distributed."""

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from ssdu.adapters import nisra
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError
from ssdu.normalization_nisra import normalize

FIXTURES = Path(__file__).parent / "fixtures" / "nisra"


def load_fixture(name="time_series"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def request_for(cube, **changes):
    return Request("nisra", cube["extension"]["matrix"], **changes)


def client_for(cube, *, after=None, data=None, **client_options):
    calls = []
    reads = 0

    def handler(request):
        nonlocal reads
        calls.append(request)
        if "/ReadMetadata/" in request.url.path:
            raise AssertionError("Incorrect endpoint")
        if "Cube_API.ReadMetadata/" in request.url.path:
            reads += 1
            result = copy.deepcopy(after if reads == 2 and after is not None else cube)
            result["value"] = []
            result.pop("status", None)
            return httpx.Response(200, json=result)
        if "Cube_API.ReadDataset/" in request.url.path:
            return httpx.Response(200, json=data if data is not None else cube)
        raise AssertionError(f"Unexpected request {request}")

    return HttpClient(
        transport=httpx.MockTransport(handler),
        retries=0,
        allowed_hosts=frozenset({"ws-data.nisra.gov.uk"}),
        **client_options,
    ), calls


def acquire(name="time_series", **request_options):
    cube = load_fixture(name)
    client, calls = client_for(cube)
    with client:
        acquired = nisra.fetch(request_for(cube, **request_options), client)
    return acquired, calls


def test_dense_time_series_semantics():
    acquired, calls = acquire()
    dataset = acquired.dataset
    rows = dataset.to_pandas()
    assert rows["tlist_a1"].tolist() == ["2023", "2024"]
    assert rows["value"].tolist() == [12.0, 13.0]
    assert rows["value_raw"].tolist() == ["12", "13"]
    assert rows["measure"].tolist() == ["COUNT", "COUNT"]
    assert rows["unit"].tolist() == ["People", "People"]
    assert rows["ni"].tolist() == ["N_SYNTH_NI", "N_SYNTH_NI"]
    assert dataset.contract["quality"]["completeness"] == "complete"
    assert {x["kind"] for x in dataset.contract["dimensions"]} == {"geography", "time"}
    assert acquired.release["revision_stable_during_retrieval"] is True
    assert acquired.release["requested_revision_pinned"] is False
    assert len(calls) == 3


@pytest.mark.parametrize(
    "copyright,expected",
    [
        ({"name": "Invented copyright owner"}, None),
        (
            {"name": "Invented copyright owner", "href": "https://terms.example.invalid/policy"},
            "https://terms.example.invalid/policy",
        ),
        ({"name": "Invented copyright owner", "href": "relative/policy"}, None),
        ({"name": "Invented copyright owner", "href": "javascript:alert(1)"}, None),
        (
            {
                "name": "Invented copyright owner",
                "href": "https://user:secret@terms.example.invalid/policy",
            },
            None,
        ),
        ({"name": "Invented copyright owner", "href": {"unexpected": "object"}}, None),
    ],
)
def test_licence_requires_policy_url_not_copyright_owner(copyright, expected):
    cube = load_fixture()
    cube["extension"]["copyright"] = copyright
    client, _ = client_for(cube)
    with client:
        acquired = nisra.fetch(request_for(cube), client)
    assert acquired.licence == (
        expected or "Not stated in NISRA API metadata; consult source terms"
    )
    assert acquired.dataset.contract["source"]["licence"] == acquired.licence
    assert acquired.metadata["cube_metadata"]["extension"]["copyright"] == copyright


def test_multidimensional_order_units_and_notes():
    acquired, _ = acquire("multidimensional")
    rows = acquired.dataset.to_pandas()
    assert rows["value"].tolist()[:8] == [1, 2, 3, 4, 100, 200, 300, 400]
    assert rows.iloc[0][["area", "year", "sex", "measure", "unit"]].tolist() == [
        "002",
        "2024",
        "F",
        "PCT",
        "Percent",
    ]
    assert rows.iloc[4]["unit"] == "People"
    assert pd.isna(rows.iloc[13]["value"])
    assert rows.iloc[13]["status"] == "c"
    assert rows.iloc[0]["notes"] == "Invented dataset note\nSEX=F: Invented category note"
    assert rows.iloc[1]["notes"] == "Invented dataset note"
    assert rows["area"].unique().tolist() == ["002", "001"]
    assert len(acquired.dataset.contract["variables"]) == 2


def test_sparse_missing_status_and_source_strings():
    acquired, _ = acquire("sparse")
    rows = acquired.dataset.to_pandas()
    assert rows["year"].tolist() == ["2022", "2023", "2024"]
    assert rows["value"].isna().tolist() == [True, True, False]
    assert rows["value_raw"].tolist() == [None, None, "001"]
    assert rows["value"].iloc[2] == 1
    assert rows["status"].tolist() == ["c", "m", "p"]
    assert (
        acquired.metadata["cube_metadata"]["extension"]["status"]["c"] == "Synthetic confidential"
    )


def test_native_selectors_preserve_cube_positions_and_release():
    acquired, calls = acquire(
        "multidimensional",
        params={"selectors": {"AREA": ["001"], "YEAR": ["2024"], "STATISTIC": ["COUNT"]}},
        max_rows=2,
        page_size=1,
    )
    assert acquired.frame["cell_index"].tolist() == [12, 13]
    assert acquired.dataset.to_pandas()["value"].isna().tolist() == [False, True]
    assert acquired.metadata["full_cube_cells"] == 16
    assert acquired.metadata["provider_total_rows"] == 2
    assert all(not request.url.params for request in calls)


def test_pure_transform_uses_only_retained_source(monkeypatch):
    acquired, _ = acquire("multidimensional")
    expected = acquired.dataset
    monkeypatch.setattr(
        HttpClient, "get", lambda *_a, **_k: pytest.fail("network in pure transform")
    )
    actual = normalize(acquired, request_for(load_fixture("multidimensional")))
    assert actual.contract == expected.contract
    assert actual.to_arrow().equals(expected.to_arrow())


@pytest.mark.parametrize(
    "params",
    [
        {"url": "https://example.invalid"},
        {"language": "xx"},
        {"max_cube_cells": 0},
        {"max_cube_cells": 100001},
        {"max_cube_cells": True},
        {"max_cube_cells": "10"},
        {"selectors": []},
        {"selectors": {"YEAR": []}},
        {"selectors": {"YEAR": [1]}},
        {"selectors": {"YEAR": ["2024", "2024"]}},
        {"selectors": {"": ["2024"]}},
    ],
)
def test_invalid_request_params_rejected_before_network(params):
    cube = load_fixture()
    client, calls = client_for(cube)
    with client, pytest.raises(RetrievalError):
        nisra.fetch(request_for(cube, params=params), client)
    assert calls == []


@pytest.mark.parametrize(
    "matrix", ["../SYNTH_TS", "https://bad.invalid", "lowercase", "A/B", "", "A" * 65]
)
def test_invalid_matrix_rejected(matrix):
    if not matrix:
        with pytest.raises(ValueError):
            Request("nisra", matrix)
        return
    with pytest.raises(RetrievalError):
        nisra.validate_request(Request("nisra", matrix))


@pytest.mark.parametrize(
    "params,match",
    [
        ({"max_cube_cells": 1}, "full cube"),
        ({"selectors": {"UNKNOWN": ["x"]}}, "unknown axis"),
        ({"selectors": {"TLIST(A1)": ["2999"]}}, "unknown or empty"),
    ],
)
def test_preflight_rejects_without_cube_download(params, match):
    cube = load_fixture()
    client, calls = client_for(cube)
    with client, pytest.raises(RetrievalError, match=match):
        nisra.fetch(request_for(cube, params=params), client)
    assert len(calls) == 1


def test_selected_row_limit_checked_before_data():
    cube = load_fixture()
    client, calls = client_for(cube)
    with client, pytest.raises(RetrievalError, match="max_rows=1"):
        nisra.fetch(request_for(cube, max_rows=1), client)
    assert len(calls) == 1


@pytest.mark.parametrize("stage", ["data", "after"])
@pytest.mark.parametrize("change", ["updated", "labels", "ordering", "units"])
def test_release_and_interpretation_changes_rejected(stage, change):
    cube = load_fixture()
    changed = copy.deepcopy(cube)
    if change == "updated":
        changed["updated"] = "2024-03-01T00:00:00Z"
    elif change == "labels":
        changed["dimension"]["TLIST(A1)"]["category"]["label"]["2023"] = "Different"
    elif change == "ordering":
        changed["dimension"]["TLIST(A1)"]["category"]["index"].reverse()
    else:
        changed["dimension"]["STATISTIC"]["category"]["unit"]["COUNT"]["label"] = "Thousands"
    client, _ = client_for(cube, **{stage: changed})
    with client, pytest.raises(RetrievalError, match="changed during retrieval"):
        nisra.fetch(request_for(cube), client)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_axes",
        "wrong_size",
        "bool_size",
        "missing_dimension",
        "wrong_positions",
        "duplicate_categories",
        "invalid_labels",
        "unknown_role",
        "overlapping_role",
        "multiple_metrics",
        "wrong_matrix",
        "missing_release",
        "wrong_class",
        "wrong_version",
        "bad_unit",
    ],
)
def test_malformed_metadata_rejected(change):
    cube = load_fixture()
    if change == "duplicate_axes":
        cube["id"] = ["NI", "NI", "STATISTIC"]
    elif change == "wrong_size":
        cube["size"] = [1, 2]
    elif change == "bool_size":
        cube["size"][0] = True
    elif change == "missing_dimension":
        cube["dimension"].pop("NI")
    elif change == "wrong_positions":
        cube["dimension"]["TLIST(A1)"]["category"]["index"] = {"2023": 1, "2024": 1}
    elif change == "duplicate_categories":
        cube["dimension"]["TLIST(A1)"]["category"]["index"] = ["2023", "2023"]
    elif change == "invalid_labels":
        cube["dimension"]["NI"]["category"]["label"]["unknown"] = "Bad"
    elif change == "unknown_role":
        cube["role"]["time"] = ["unknown"]
    elif change == "overlapping_role":
        cube["role"]["geo"] = ["TLIST(A1)"]
    elif change == "multiple_metrics":
        cube["role"] = {"metric": ["STATISTIC", "NI"]}
    elif change == "wrong_matrix":
        cube["extension"]["matrix"] = "OTHER"
    elif change == "missing_release":
        cube.pop("updated")
    elif change == "wrong_class":
        cube["class"] = "collection"
    elif change == "wrong_version":
        cube["version"] = "1.0"
    else:
        cube["dimension"]["STATISTIC"]["category"]["unit"]["COUNT"]["label"] = 1
    client, _ = client_for(cube)
    with client, pytest.raises(RetrievalError):
        nisra.fetch(Request("nisra", "SYNTH_TS"), client)


@pytest.mark.parametrize(
    "values", [[], [1], [True, 2], [float("inf"), 2], {"02": 1}, {"2": 1}, [[], 2]]
)
def test_invalid_cube_values_fail_closed(values):
    cube = load_fixture()
    bad = copy.deepcopy(cube)
    bad["value"] = values
    client, _ = client_for(cube, data=bad)
    with client, pytest.raises((RetrievalError, ValueError)):
        nisra.fetch(request_for(cube), client)


@pytest.mark.parametrize("status", [[], ["p"], [1, None], {"3": "p"}, {"00": "p"}, 1])
def test_invalid_status_fails_closed(status):
    cube = load_fixture()
    cube["status"] = status
    client, _ = client_for(cube)
    with client, pytest.raises(RetrievalError):
        nisra.fetch(request_for(cube), client)


def test_uniform_status_and_delivery_link_difference():
    cube = load_fixture()
    cube["status"] = "p"
    data = copy.deepcopy(cube)
    data["href"] = "https://example.invalid/not-followed"
    data["link"] = {"alternate": [{"href": "https://example.invalid/not-followed"}]}
    client, _ = client_for(cube, data=data)
    with client:
        acquired = nisra.fetch(request_for(cube), client)
    assert acquired.dataset.to_pandas()["status"].tolist() == ["p", "p"]


@pytest.mark.parametrize(
    "mutation", ["position", "code", "missing_code", "duplicate", "count", "selectors"]
)
def test_retained_source_coordinate_corruption_rejected(mutation):
    acquired, _ = acquire()
    if mutation == "position":
        acquired.frame.loc[0, "cell_index"] = 9
    elif mutation == "code":
        acquired.frame.loc[0, "TLIST(A1)"] = "2024"
    elif mutation == "missing_code":
        acquired.frame.loc[0, "TLIST(A1)"] = pd.NA
    elif mutation == "duplicate":
        acquired.frame.loc[1, "cell_index"] = 0
    elif mutation == "selectors":
        acquired.metadata["selectors"] = None
    else:
        acquired.frame = acquired.frame.iloc[:1]
    with pytest.raises(RetrievalError):
        normalize(acquired)


def test_request_and_byte_budgets_enforced_by_shared_client():
    cube = load_fixture()
    client, _ = client_for(cube, max_requests=1)
    with client, pytest.raises(RetrievalError, match="request budget"):
        nisra.fetch(request_for(cube), client)
    client, _ = client_for(cube, max_bytes=10)
    with client, pytest.raises(RetrievalError, match="Response exceeds"):
        nisra.fetch(request_for(cube), client)


def test_search_uses_keyword_server_query_and_bounded_result():
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["params"] == {"Search": "invented", "LngIsoCode": "en"}
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "ssdu-search",
                "result": [{"MtrCode": f"SYNTH_{i}", "MtrTitle": "Invented"} for i in range(30)],
            },
        )

    with HttpClient(
        transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"ws-data.nisra.gov.uk"})
    ) as client:
        result = nisra.search("invented", client)
        assert len(result) == 25
        assert result[0] == {"id": "SYNTH_0", "title": "Invented", "provider": "nisra"}
        with pytest.raises(ValueError):
            nisra.search("", client)
    assert len(calls) == 1


def test_fixture_manifest_is_synthetic_and_hashes_match():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["kind"] == "independently_invented_synthetic"
    for entry in manifest["fixtures"]:
        raw = (FIXTURES / entry["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert len(raw) == entry["bytes"]
        assert entry["references"]
        assert json.loads(raw)["extension"]["ssdu_fixture"].startswith("synthetic")
