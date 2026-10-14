"""Semantic conformance against independently invented Scottish RDF cube fixtures."""

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from ssdu.adapters.scotland import (
    AREA,
    BASE,
    MAX_METADATA_ROWS,
    QB,
    STATUS,
    UNIT,
    default_request,
    fetch,
    metadata_query,
    observations_query,
    search,
    validate_request,
)
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError
from ssdu.normalization_scotland import normalize

FIXTURES = Path(__file__).parent / "fixtures" / "scotland"


def fixture(name="series"):
    path = FIXTURES / name
    return tuple(
        json.loads((path / filename).read_text())
        for filename in ("request.json", "metadata.json", "observations.json")
    )


def client_for(metadata, observations, calls=None):
    def handler(request):
        assert request.url.host == "statistics.gov.scot"
        assert request.url.path == "/sparql.json"
        if calls is not None:
            calls.append(request)
        query = request.url.params["query"]
        return httpx.Response(200, json=metadata if "AS ?kind" in query else observations)

    return HttpClient(
        transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"statistics.gov.scot"})
    )


def retrieve(name="series"):
    request, metadata, observations = fixture(name)
    calls = []
    with client_for(metadata, observations, calls) as client:
        result = fetch(Request(**request), client)
        assert len(client.resources) == 3
    assert len(calls) == 3
    assert str(calls[0].url) == str(calls[-1].url)
    return result


def test_series_preserves_uri_codes_and_raw_lexemes():
    acquired = retrieve()
    dataset = acquired.dataset
    table = dataset.to_pandas()
    assert table["value"].tolist() == [7.0, 0.0]
    assert table["value_raw"].tolist() == ["0007", "0"]
    assert table["geography"].tolist() == ["https://example.invalid/ssdu/geography/001"] * 2
    assert table["time"].tolist() == [
        "http://reference.data.gov.uk/id/year/2023",
        "http://reference.data.gov.uk/id/year/2024",
    ]
    assert table["unit"].tolist() == ["https://example.invalid/ssdu/unit/count"] * 2
    assert dataset.descriptor.dimensions[0].codes[0].label == "Invented district 001"
    assert dataset.descriptor.transform.id == "ssdu.scotland.observations"
    assert dataset.descriptor.quality.completeness == "complete"
    assert acquired.release["revision_status"] == "metadata_checked_unpinned"
    assert acquired.licence == "https://example.invalid/ssdu/synthetic-fixtures"
    assert normalize(acquired).to_arrow().equals(dataset.to_arrow())


def test_multiple_measures_preserve_variable_identities():
    acquired = retrieve("multiple-measures")
    table = acquired.dataset.to_pandas()
    assert len(acquired.frame) == 2 and len(table) == 4
    assert (
        table["measure"].tolist()
        == [
            "https://example.invalid/ssdu/measure/count",
            "https://example.invalid/ssdu/measure/rate",
        ]
        * 2
    )
    assert table["value"].tolist() == [12, 2.5, 18, 3.75]
    assert (
        table["group"].tolist()
        == ["https://example.invalid/ssdu/group/A"] * 2
        + ["https://example.invalid/ssdu/group/B"] * 2
    )
    assert len(acquired.dataset.descriptor.variables) == 2


def test_suppression_status_and_notes_are_retained():
    acquired = retrieve("suppressed")
    table = acquired.dataset.to_pandas()
    assert table["value_raw"].tolist() == ["..", "1,234"]
    assert table["value"].isna().tolist() == [True, False]
    assert table.iloc[1]["value"] == 1234
    assert table["status"].tolist() == [
        "https://example.invalid/ssdu/status/suppressed",
        "https://example.invalid/ssdu/status/provisional",
    ]
    assert table.iloc[0]["notes"] == "Synthetic small-cell suppression"
    assert any(
        item.code == "non_numeric_value" for item in acquired.dataset.descriptor.quality.diagnostics
    )
    assert "measure_type" not in table.columns


def test_fixtures_have_hashes_and_no_publisher_records():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["fixture_kind"] == "independently_invented_synthetic"
    assert manifest["contains_publisher_records"] is False
    assert len(manifest["files"]) == 9
    for entry in manifest["files"]:
        body = (FIXTURES / entry["path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
        assert len(body) == entry["bytes"]
        assert entry["reference"].startswith("https://guides.statistics.gov.scot/")
    assert all(
        json.loads(path.read_text())["dataset_id"].startswith("ssdu-synthetic-")
        for path in FIXTURES.glob("*/request.json")
    )


@pytest.mark.parametrize(
    "bad_request",
    [
        Request("scotland", "../cube"),
        Request("scotland", "valid", {"query": "SELECT *"}),
        Request(
            "scotland",
            "valid",
            {"dimensions": {AREA: ["https://example.invalid/> } UNION { ?s ?p ?o"]}},
        ),
        Request(
            "scotland",
            "valid",
            {"dimensions": {AREA: ["https://example.invalid/a", "https://example.invalid/a"]}},
        ),
        Request("scotland", "valid", {"dimensions": {AREA: [{}]}}),
        Request("scotland", "valid", {"dimensions": {AREA: []}}),
        Request("scotland", "valid", {"dimensions": []}),
        Request("scotland", "valid", {"expected_modified": 2024}),
        Request("nomis", "valid"),
    ],
)
def test_invalid_requests_are_rejected_before_network(bad_request):
    with (
        HttpClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("network attempted")),
            allowed_hosts=frozenset({"statistics.gov.scot"}),
        ) as client,
        pytest.raises(RetrievalError),
    ):
        fetch(bad_request, client)


def test_queries_are_bounded_deterministic_and_atomic():
    request = default_request()
    validate_request(request)
    query = observations_query(request)
    assert "OFFSET" not in query and "LIMIT 101" in query and "LIMIT 6401" in query
    assert "ORDER BY ?obs ?property ?value ?label" in query
    assert "LIMIT 1001" in metadata_query(request.dataset_id)
    assert "S92000003" in query


def test_unknown_dimension_rejected_before_observation_fetch():
    request, metadata, observations = fixture()
    request["params"] = {
        "dimensions": {"https://example.invalid/unknown": ["https://example.invalid/member"]}
    }
    calls = []
    with (
        client_for(metadata, observations, calls) as client,
        pytest.raises(RetrievalError, match="Unknown Scotland dimension"),
    ):
        fetch(Request(**request), client)
    assert len(calls) == 1


def test_expected_modified_checks_source_metadata():
    request, metadata, observations = fixture()
    request["params"] = {"expected_modified": "wrong"}
    with (
        client_for(metadata, observations) as client,
        pytest.raises(RetrievalError, match="requested release"),
    ):
        fetch(Request(**request), client)


def test_revision_change_aborts():
    request, metadata, observations = fixture()
    calls = 0

    def handler(response):
        nonlocal calls
        calls += 1
        value = copy.deepcopy(
            metadata if "AS ?kind" in response.url.params["query"] else observations
        )
        if calls == 3:
            value["results"]["bindings"][0]["value"]["value"] = "Changed title"
        return httpx.Response(200, json=value)

    with (
        HttpClient(
            transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"statistics.gov.scot"})
        ) as client,
        pytest.raises(RetrievalError, match="changed during retrieval"),
    ):
        fetch(Request(**request), client)


def test_semantically_identical_metadata_order_does_not_abort():
    request, metadata, observations = fixture()
    calls = 0

    def handler(response):
        nonlocal calls
        calls += 1
        value = copy.deepcopy(
            metadata if "AS ?kind" in response.url.params["query"] else observations
        )
        if calls == 3:
            value["results"]["bindings"].reverse()
        return httpx.Response(200, json=value)

    with HttpClient(
        transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"statistics.gov.scot"})
    ) as client:
        assert len(fetch(Request(**request), client).frame) == 2


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("too_many_rows", "max_rows"),
        ("missing_dimension", "missing a required"),
        ("different_dataset", "different dataset"),
        ("literal_dimension", "URI member codes"),
        ("multiple_values", "multiple RDF values"),
        ("missing_measure", "neither a measure"),
        ("unknown_measure", "undeclared measureType"),
        ("malformed_term", "Unsupported Scotland RDF"),
    ],
)
def test_incomplete_or_unsupported_observations_fail_closed(mutation, match):
    request, metadata, observations = fixture("suppressed")
    rows = observations["results"]["bindings"]
    if mutation == "too_many_rows":
        request["max_rows"] = 1
    elif mutation == "missing_dimension":
        observations["results"]["bindings"] = [
            row for row in rows if row["property"]["value"] != AREA
        ]
    elif mutation == "different_dataset":
        next(row for row in rows if row["property"]["value"] == QB + "dataSet")["value"][
            "value"
        ] = BASE + "wrong"
    elif mutation == "literal_dimension":
        next(row for row in rows if row["property"]["value"] == AREA)["value"]["type"] = "literal"
    elif mutation == "multiple_values":
        row = copy.deepcopy(next(row for row in rows if row["property"]["value"] == AREA))
        row["value"]["value"] += "-different"
        rows.append(row)
    elif mutation == "missing_measure":
        observations["results"]["bindings"] = [
            row
            for row in rows
            if row["property"]["value"]
            not in {"https://example.invalid/ssdu/measure/count", STATUS}
        ]
    elif mutation == "unknown_measure":
        next(row for row in rows if row["property"]["value"] == QB + "measureType")["value"][
            "value"
        ] = "https://example.invalid/unknown"
    elif mutation == "malformed_term":
        rows[0]["value"]["type"] = "bnode"
    with client_for(metadata, observations) as client, pytest.raises(RetrievalError, match=match):
        fetch(Request(**request), client)


@pytest.mark.parametrize(
    "mutation,match",
    [("file", "file datasets"), ("slice", "slice-attached"), ("over_limit", "metadata exceeds")],
)
def test_unsupported_metadata_fail_closed(mutation, match):
    request, metadata, observations = fixture()
    rows = metadata["results"]["bindings"]
    if mutation == "file":
        metadata["results"]["bindings"] = [
            row
            for row in rows
            if row["kind"]["value"] == "dataset" and row["property"]["value"] != QB + "structure"
        ]
    elif mutation == "slice":
        next(row for row in rows if row["kind"]["value"] == QB + "attribute")["attachment"] = {
            "type": "uri",
            "value": QB + "Slice",
        }
    else:
        metadata["results"]["bindings"] = rows * (MAX_METADATA_ROWS // len(rows) + 1)
    with client_for(metadata, observations) as client, pytest.raises(RetrievalError, match=match):
        fetch(Request(**request), client)


def test_response_cell_bound_is_not_silently_truncated():
    request, metadata, observations = fixture()
    request["max_rows"] = 1
    observations["results"]["bindings"] *= 1001
    with (
        client_for(metadata, observations) as client,
        pytest.raises(RetrievalError, match="cell bound"),
    ):
        fetch(Request(**request), client)


def test_selected_members_are_verified_in_returned_observations():
    request, metadata, observations = fixture()
    request["params"] = {"dimensions": {AREA: ["https://example.invalid/ssdu/geography/999"]}}
    with (
        client_for(metadata, observations) as client,
        pytest.raises(RetrievalError, match="violates requested"),
    ):
        fetch(Request(**request), client)


def test_unknown_units_remain_unknown():
    request, metadata, observations = fixture()
    observations["results"]["bindings"] = [
        row for row in observations["results"]["bindings"] if row["property"]["value"] != UNIT
    ]
    with client_for(metadata, observations) as client:
        result = fetch(Request(**request), client)
    assert result.dataset.to_pandas()["unit"].isna().all()
    assert result.dataset.descriptor.variables[0].unit is None


def test_empty_selection_has_typed_observations():
    request, metadata, observations = fixture()
    observations["results"]["bindings"] = []
    with client_for(metadata, observations) as client:
        result = fetch(Request(**request), client)
    assert len(result.frame) == 0 and result.dataset.to_arrow().num_rows == 0
    assert str(result.dataset.to_arrow().schema.field("value").type) == "double"


def test_search_uses_safe_literal_and_bounds():
    def handler(request):
        query = request.url.params["query"]
        assert "LIMIT 25" in query and '\\"' in query
        return httpx.Response(
            200,
            json={
                "head": {"vars": ["dataset", "title"]},
                "results": {
                    "bindings": [
                        {
                            "dataset": {"type": "uri", "value": BASE + "ssdu-synthetic-series"},
                            "title": {"type": "literal", "value": "Synthetic series"},
                        }
                    ]
                },
            },
        )

    with HttpClient(
        transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"statistics.gov.scot"})
    ) as client:
        assert search('title " quoted', client) == [
            {"id": "ssdu-synthetic-series", "title": "Synthetic series", "provider": "scotland"}
        ]


def test_missing_dataset_identity_is_incomplete():
    request, metadata, observations = fixture()
    observations["results"]["bindings"] = [
        row
        for row in observations["results"]["bindings"]
        if row["property"]["value"] != QB + "dataSet"
    ]
    with (
        client_for(metadata, observations) as client,
        pytest.raises(RetrievalError, match="missing its dataset identity"),
    ):
        fetch(Request(**request), client)


def test_dataset_attached_unit_is_resolved_from_retained_metadata():
    request, metadata, observations = fixture()
    metadata["results"]["bindings"].append(
        {
            "kind": {"type": "literal", "value": "dataset"},
            "property": {"type": "uri", "value": UNIT},
            "value": {"type": "uri", "value": "https://example.invalid/dataset-unit"},
        }
    )
    definition = next(
        row for row in metadata["results"]["bindings"] if row["kind"]["value"] == QB + "attribute"
    )
    definition["attachment"] = {"type": "uri", "value": QB + "DataSet"}
    observations["results"]["bindings"] = [
        row for row in observations["results"]["bindings"] if row["property"]["value"] != UNIT
    ]
    with client_for(metadata, observations) as client:
        result = fetch(Request(**request), client)
    assert (
        result.dataset.to_pandas()["unit"].tolist() == ["https://example.invalid/dataset-unit"] * 2
    )


def test_measure_type_cannot_silently_drop_another_measure_value():
    request, metadata, observations = fixture("multiple-measures")
    metadata["results"]["bindings"].append(
        {
            "kind": {"type": "literal", "value": QB + "dimension"},
            "property": {"type": "uri", "value": QB + "measureType"},
            "value": {"type": "uri", "value": QB + "measureType"},
        }
    )
    for obs in sorted({row["obs"]["value"] for row in observations["results"]["bindings"]}):
        observations["results"]["bindings"].append(
            {
                "obs": {"type": "uri", "value": obs},
                "property": {"type": "uri", "value": QB + "measureType"},
                "value": {"type": "uri", "value": "https://example.invalid/ssdu/measure/count"},
            }
        )
    with (
        client_for(metadata, observations) as client,
        pytest.raises(RetrievalError, match="conflict with measureType"),
    ):
        fetch(Request(**request), client)


@pytest.mark.parametrize(
    "payload",
    [
        {"head": {"vars": [{}]}, "results": {"bindings": []}},
        {
            "head": {"vars": ["kind", "property", "value"]},
            "results": {"bindings": [{"kind": {"type": "literal", "value": "dataset"}}]},
        },
    ],
)
def test_malformed_select_results_raise_retrieval_error(payload):
    with client_for(payload, payload) as client, pytest.raises(RetrievalError):
        fetch(Request(**fixture()[0]), client)


@pytest.mark.parametrize("name", ["series", "multiple-measures", "suppressed"])
def test_store_cache_and_offline_open_share_dataset_contract(name, tmp_path):
    from ssdu.store import Store

    request, metadata, observations = fixture(name)
    calls = []
    store = Store(tmp_path / "store")
    with client_for(metadata, observations, calls) as client:
        first = store.fetch_dataset(Request(**request), client=client)
        cached = store.fetch_dataset(Request(**request), client=client)
    assert len(calls) == 3
    with HttpClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network attempted")),
        allowed_hosts=frozenset({"statistics.gov.scot"}),
    ) as client:
        offline = store.fetch_dataset(Request(**request), offline=True, client=client)
        assert client.attempts == 0
    assert first.snapshot_id == cached.snapshot_id == offline.snapshot_id
    assert first.to_arrow().equals(offline.to_arrow())
    assert len(store.verify(first.snapshot_id)["resources"]) == 3
