"""Independently invented responses; no NISRA data or metadata captures.

Protocol references:
https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful
https://json-stat.org/format/
"""

import copy
import json
from pathlib import Path

import httpx
import pytest

from ssdu import Connector
from ssdu.adapters import nisra
from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError


def cube():
    result = json.loads((Path(__file__).parent / "fixtures/nisra/time_series.json").read_text())
    result["extension"]["matrix"] = "SYNTH_CONNECTOR"
    return result


def metadata(result):
    result = copy.deepcopy(result)
    result.pop("value", None)
    result.pop("status", None)
    return result


def collection(count=31):
    return {
        "class": "collection",
        "version": "2.0",
        "link": {
            "item": [
                {
                    "extension": {"matrix": f"SYNTH_{index:03d}"},
                    "label": f"Invented table {index}",
                }
                for index in reversed(range(count))
            ]
        },
    }


def client_for(document):
    calls = []

    def handler(request):
        calls.append(request)
        if "ReadCollection" in request.url.path:
            return httpx.Response(200, json=document)
        assert "ReadMetadata/SYNTH_CONNECTOR/en" in request.url.path
        return httpx.Response(200, json=metadata(document))

    return HttpClient(transport=httpx.MockTransport(handler), retries=0), calls


@pytest.mark.parametrize("envelope", [False, True])
def test_catalogue_has_truthful_continuation_beyond_25_and_local_search(envelope):
    document = collection()
    if envelope:
        document = {"data": document, "jsonrpc": "2.0"}
    client, calls = client_for(document)
    with client:
        first = nisra.list_datasets("", client, limit=20)
        second = nisra.list_datasets("", client, limit=20, offset=first["next_offset"])
        matches = nisra.list_datasets("030", client)
        empty = nisra.list_datasets("", client, offset=40)
    assert first["total"] == 31 and first["next_offset"] == 20
    assert first["complete_catalogue"] and first["catalogue_total"] == 31
    assert first["items"][0]["id"] == "SYNTH_000"
    assert len(second["items"]) == 11 and second["next_offset"] is None
    assert matches["items"] == [
        {"id": "SYNTH_030", "title": "Invented table 30", "provider": "nisra"}
    ]
    assert empty["items"] == [] and empty["next_offset"] is None
    assert len(calls) == 4


@pytest.mark.parametrize("change", ["duplicate", "matrix", "title", "class", "items", "error"])
def test_malformed_catalogue_fails_closed(change):
    document = collection(2)
    items = document["link"]["item"]
    if change == "duplicate":
        items.append(copy.deepcopy(items[0]))
    elif change == "matrix":
        items[0]["extension"]["matrix"] = "../invalid"
    elif change == "title":
        items[0]["label"] = None
    elif change == "class":
        document["class"] = "dataset"
    elif change == "items":
        document["link"]["item"] = {}
    else:
        document["error"] = {"message": "Invented failure"}
    client, _ = client_for(document)
    with client, pytest.raises(RetrievalError):
        nisra.list_datasets("", client)


def test_description_and_native_code_pages_are_metadata_only():
    document = cube()
    # Dict insertion order deliberately disagrees with category index positions.
    category = document["dimension"]["TLIST(A1)"]["category"]
    category["index"] = {"2024": 1, "2023": 0}
    category["label"] = {"2024": "Invented later year", "2023": "Invented earlier year"}
    client, calls = client_for(document)
    with client:
        descriptor = nisra.describe_dataset("SYNTH_CONNECTOR", client)
        first = nisra.list_dimension_values("SYNTH_CONNECTOR", "TLIST(A1)", client, limit=1)
        second = nisra.list_dimension_values(
            "SYNTH_CONNECTOR", "TLIST(A1)", client, limit=1, offset=first["next_offset"]
        )
        matches = nisra.list_dimension_values(
            "SYNTH_CONNECTOR", "TLIST(A1)", client, search="LATER"
        )
    assert descriptor["dimensions"][1]["id"] == "TLIST(A1)"
    assert descriptor["dimensions"][1]["value_count"] == 2
    assert descriptor["full_cube_cells"] == 2
    assert descriptor["capabilities"]["server_side_selection"] is False
    assert descriptor["capabilities"]["page_size_paginates_provider"] is False
    assert descriptor["release"]["updated"] == document["updated"]
    assert "value" not in descriptor["source_metadata"]
    assert "status" not in descriptor["source_metadata"]
    assert first["items"] == [{"value": "2023", "label": "Invented earlier year"}]
    assert second["items"] == [{"value": "2024", "label": "Invented later year"}]
    assert second["scope"] == "provider" and second["next_offset"] is None
    assert matches["items"] == second["items"]
    assert len(calls) == 4


def test_large_cube_can_be_inspected_but_cannot_be_planned_or_downloaded():
    document = cube()
    document["size"] = [1, 400, 400]
    for axis in ["TLIST(A1)", "NI"]:
        document["dimension"][axis]["category"] = {
            "index": [f"SYNTH_{index}" for index in range(400)]
        }
    client, calls = client_for(document)
    with client:
        descriptor = nisra.describe_dataset("SYNTH_CONNECTOR", client)
        codes = nisra.list_dimension_values("SYNTH_CONNECTOR", "NI", client, offset=390)
        with pytest.raises(RetrievalError, match="full cube has 160000"):
            nisra.validate_selection(Request("nisra", "SYNTH_CONNECTOR"), descriptor)
        with pytest.raises(RetrievalError, match="full cube has 160000"):
            nisra.fetch(Request("nisra", "SYNTH_CONNECTOR"), client)
    assert descriptor["full_cube_cells"] == 160_000
    assert not descriptor["capabilities"]["within_retrieval_cell_limit"]
    assert len(codes["items"]) == 10
    assert len(calls) == 3


@pytest.mark.parametrize(
    "params,match",
    [
        ({"selectors": {"TLIST(A1)": ["2999"]}}, "unknown or empty"),
        ({"selectors": {"invented": ["2023"]}}, "unknown axis"),
        ({"language": "ga"}, "language=en"),
        ({"max_cube_cells": 1}, "full cube"),
    ],
)
def test_selection_validates_metadata_codes_and_limits(params, match):
    client, calls = client_for(cube())
    with client:
        descriptor = nisra.describe_dataset("SYNTH_CONNECTOR", client)
        with pytest.raises(RetrievalError, match=match):
            nisra.validate_selection(Request("nisra", "SYNTH_CONNECTOR", params), descriptor)
        nisra.validate_selection(
            Request("nisra", "SYNTH_CONNECTOR", {"selectors": {"TLIST(A1)": ["2023"]}}),
            descriptor,
        )
        with pytest.raises(RetrievalError, match="max_rows=1"):
            nisra.validate_selection(Request("nisra", "SYNTH_CONNECTOR", max_rows=1), descriptor)
        with pytest.raises(RetrievalError, match="different dataset"):
            nisra.validate_selection(Request("nisra", "SYNTH_OTHER"), descriptor)
    assert len(calls) == 1


def test_metadata_cache_hits_need_no_network_or_quota_and_replay_offline(tmp_path):
    client, calls = client_for(cube())
    with client:
        with CachedClient(tmp_path, client=client) as cached:
            expected = nisra.describe_dataset("SYNTH_CONNECTOR", cached)
        before = len(calls)
        with CachedClient(tmp_path, client=client) as cached:
            assert nisra.describe_dataset("SYNTH_CONNECTOR", cached) == expected
        assert len(calls) == before == 1
        with CachedClient(tmp_path, offline=True) as cached:
            assert nisra.describe_dataset("SYNTH_CONNECTOR", cached) == expected
            values = nisra.list_dimension_values("SYNTH_CONNECTOR", "TLIST(A1)", cached)
        assert values["total"] == 2
        assert len(calls) == 1


def test_nisra_connector_workflow_metadata_selection_fetch_and_offline(tmp_path):
    """Common API acceptance test, using independently invented statistics only."""
    current, calls = cube(), []

    def handler(request):
        calls.append(request)
        if "ReadCollection" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "class": "collection",
                    "version": "2.0",
                    "link": {
                        "item": [{"extension": current["extension"], "label": current["label"]}]
                    },
                },
            )
        return httpx.Response(
            200, json=metadata(current) if "ReadMetadata" in request.url.path else current
        )

    selection = Request(
        "nisra", "SYNTH_CONNECTOR", {"selectors": {"TLIST(A1)": ["2024"]}}, max_rows=1
    )
    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        connector = Connector("nisra", tmp_path, client=client)
        catalogue = connector.list_datasets()
        descriptor = connector.describe_dataset("SYNTH_CONNECTOR")
        codes = connector.list_dimension_values("SYNTH_CONNECTOR", "TLIST(A1)")
        plan = connector.plan_fetch(selection)
        assert all("ReadDataset" not in str(call.url) for call in calls)
        assert catalogue["items"][0]["id"] == descriptor["dataset_id"]
        assert codes["items"][-1]["value"] == "2024"
        assert plan["selector_validation"] == "metadata"
        dataset = connector.fetch_dataset(selection)
        assert dataset.to_arrow().to_pydict()["value_raw"] == ["13"]
        before = len(calls)
        offline = Connector("nisra", tmp_path)
        assert offline.list_datasets(offline=True) == catalogue
        assert offline.describe_dataset("SYNTH_CONNECTOR", offline=True) == descriptor
        assert offline.list_dimension_values("SYNTH_CONNECTOR", "TLIST(A1)", offline=True) == codes
        assert offline.plan_fetch(selection, offline=True) == plan
        assert offline.fetch_dataset(selection, offline=True).to_arrow().equals(dataset.to_arrow())
        assert len(calls) == before


def test_complete_cube_reused_across_selections_with_live_final_metadata_check(tmp_path):
    document, calls = cube(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=metadata(document) if "ReadMetadata" in request.url.path else document
        )

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        for year in ["2023", "2024"]:
            request = Request(
                "nisra", "SYNTH_CONNECTOR", {"selectors": {"TLIST(A1)": [year]}}, max_rows=1
            )
            with CachedClient(tmp_path, client=client) as cached:
                acquired = nisra.fetch(request, cached)
            assert acquired.frame["TLIST(A1)"].tolist() == [year]
            assert [resource.cache_scope is not None for resource in cached.resources] == [
                False,
                True,
                False,
            ]
    assert len([call for call in calls if "ReadDataset" in call.url.path]) == 1
    assert len([call for call in calls if "ReadMetadata" in call.url.path]) == 3


@pytest.mark.parametrize("change", ["updated", "unit", "axis_label", "category_order"])
def test_changed_interpretation_never_reuses_old_cube_at_same_url(tmp_path, change):
    current, calls = cube(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=metadata(current) if "ReadMetadata" in request.url.path else current
        )

    request = Request("nisra", "SYNTH_CONNECTOR")
    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        with CachedClient(tmp_path, client=client) as cached:
            original = nisra.fetch(request, cached)
        original_token = next(r.cache_scope for r in cached.resources if "ReadDataset" in r.url)
        if change == "updated":
            current["updated"] = "2025-02-01T00:00:00Z"
        elif change == "unit":
            current["dimension"]["STATISTIC"]["category"]["unit"]["COUNT"]["label"] = "Items"
        elif change == "axis_label":
            current["dimension"]["NI"]["label"] = "Invented changed label"
        else:
            current["dimension"]["TLIST(A1)"]["category"]["index"].reverse()
        current["value"] = [91, 92]
        # The cached first metadata read can be stale; the required live final
        # check rejects that acquisition and records the newly observed metadata.
        with (
            CachedClient(tmp_path, client=client) as cached,
            pytest.raises(RetrievalError, match="changed during retrieval"),
        ):
            nisra.fetch(request, cached)
        with CachedClient(tmp_path, client=client) as cached:
            changed = nisra.fetch(request, cached)
        changed_token = next(r.cache_scope for r in cached.resources if "ReadDataset" in r.url)
        assert changed_token != original_token
        assert changed.frame["value_raw"].tolist() == ["91", "92"]
        assert changed.release["metadata_sha256"] != original.release["metadata_sha256"]
        assert len([call for call in calls if "ReadDataset" in call.url.path]) == 2
        before = len(calls)
        with CachedClient(tmp_path, offline=True) as cached:
            replay = nisra.fetch(request, cached)
        assert replay.dataset.to_arrow().equals(changed.dataset.to_arrow())
        assert len(calls) == before


@pytest.mark.parametrize("limit,offset", [(True, 0), (0, 0), (101, 0), (1, -1), (1, True)])
def test_invalid_bounds_fail_before_network(limit, offset):
    client, calls = client_for(cube())
    with client, pytest.raises(ValueError):
        nisra.list_datasets("", client, limit=limit, offset=offset)
    assert calls == []


def test_unknown_dimension_and_wrong_matrix_fail_without_values():
    document = cube()
    client, calls = client_for(document)
    with client:
        with pytest.raises(ValueError, match="Unknown NISRA dimension"):
            nisra.list_dimension_values("SYNTH_CONNECTOR", "YEAR", client)
        document["extension"]["matrix"] = "SYNTH_WRONG"
        with pytest.raises(RetrievalError, match="different matrix"):
            nisra.describe_dataset("SYNTH_CONNECTOR", client)
    assert len(calls) == 2
