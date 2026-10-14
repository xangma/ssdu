import csv
import hashlib
import io
import json

import httpx
import pytest
import test_nomis
import test_statswales
from pydantic import ValidationError

from ssdu.adapters import deprivation
from ssdu.contracts import QuerySpec
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.service import Service
from ssdu.shared import SharedDeprivation
from ssdu.store import Store, canonical

source_fixture = test_statswales.source_fixture


@pytest.fixture
def service(tmp_path, iod_csv, monkeypatch):
    with HttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=iod_csv))
    ) as transport:
        source = SharedDeprivation(Store(tmp_path)).source(client=transport)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    return Service(tmp_path), source["product_id"]


def test_discovery_dimensions_preview_count_and_provenance_offline(service):
    data, snapshot_id = service
    key = data.search_datasets("deprivation")["items"][0]["dataset_id"]
    dataset = data.describe_dataset(key)
    assert dataset["release"]["lsoa_geography"] == "2021"
    assert "10%" in next(c for c in dataset["columns"] if c["name"] == "imd_decile")["definition"]
    dimensions = data.list_dimension_values(key, "local_authority_code", "Portsmouth")
    assert dimensions["items"] == [{"value": "E06000044", "label": "Portsmouth"}]
    spec = QuerySpec(
        columns=["lsoa_code", "imd_score"],
        filters=[{"column": "local_authority_code", "value": "E06000044"}],
        limit=1,
    )
    result = data.preview_data(snapshot_id, spec)
    assert result["total_rows"] == 2 and result["next_offset"] == 1
    page2 = data.query_data(snapshot_id, spec.model_copy(update={"offset": 1}))
    assert page2["rows"] == [{"lsoa_code": "E01017014", "imd_score": None}]
    assert result["provenance"]["snapshot_id"] == snapshot_id
    assert data.query_data(snapshot_id, spec.model_copy(update={"count_only": True}))["rows"] == [
        {"count": 2}
    ]
    assert data.get_provenance(snapshot_id)["licence"] == deprivation.LICENCE


def test_in_numeric_and_null_filters(service):
    data, snapshot_id = service
    result = data.query_data(
        snapshot_id,
        QuerySpec(
            columns=["lsoa_code"], filters=[{"column": "imd_score", "op": "eq", "value": None}]
        ),
    )
    assert result["rows"] == [{"lsoa_code": "E01017014"}]
    assert data.query_data(
        snapshot_id,
        QuerySpec(
            filters=[
                {"column": "imd_decile", "op": "in", "value": [5, 9]},
                {"column": "imd_score", "op": "gte", "value": 8},
                {"column": "imd_score", "op": "lte", "value": 23},
            ],
            count_only=True,
        ),
    )["rows"] == [{"count": 2}]


@pytest.mark.parametrize(
    "spec",
    [
        {"columns": ["lsoa_code; DROP TABLE data"]},
        {"filters": [{"column": "local_authority_code", "value": 6000044}]},
        {"filters": [{"column": "imd_score", "value": "0 OR 1=1"}]},
        {"sort": [{"column": "unknown"}]},
    ],
)
def test_query_rejects_unsafe_or_mistyped_identifiers(service, spec):
    data, snapshot_id = service
    with pytest.raises(ValueError):
        data.query_data(snapshot_id, QuerySpec(**spec))


def test_query_bounds_and_sql_are_not_public_contracts(service):
    data, snapshot_id = service
    for spec in ({"limit": 101}, {"limit": True}, {"sql": "SELECT 1"}, {"offset": -1}):
        with pytest.raises(ValidationError):
            data.query_data(snapshot_id, spec)
    with pytest.raises(ValueError):
        data.get_provenance("../../etc/passwd")


@pytest.fixture
def statswales_service(tmp_path, source_fixture, monkeypatch):
    store = Store(tmp_path)
    client, _ = test_statswales.client_for(source_fixture)
    with client:
        snapshot = store.fetch(test_statswales.request_for(), client=client)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    return Service(tmp_path), snapshot


def test_statswales_dimension_uses_explicit_recovered_code_labels(statswales_service):
    data, manifest = statswales_service
    key = f"statswales:{manifest['dataset_id']}"
    result = data.list_dimension_values(key, "Area__code", search="Cardiff")
    assert result["items"] == [{"value": "002", "label": "Cardiff"}]


def test_statswales_numeric_filter_uses_physical_schema(statswales_service):
    data, manifest = statswales_service
    assert manifest["columns"]["Area_sort"] == "object"
    result = data.query_data(
        manifest["product_id"],
        QuerySpec(
            columns=["Area__code", "Area_sort"],
            filters=[{"column": "Area_sort", "op": "gte", "value": 2}],
            sort=[{"column": "Area_sort"}],
        ),
    )
    assert result["rows"] == [
        {"Area__code": "002", "Area_sort": 2},
        {"Area__code": "003", "Area_sort": 3},
    ]
    metadata = data.describe_dataset(f"statswales:{manifest['dataset_id']}")
    assert result["provenance"]["metadata_version"] == metadata["metadata_version"]
    assert (
        next(c for c in metadata["columns"] if c["name"] == "Area_sort")["storage_dtype"] == "int64"
    )
    with pytest.raises(ValueError, match="Identifier/text"):
        data.query_data(
            manifest["product_id"], QuerySpec(filters=[{"column": "Area__code", "value": 2}])
        )


def test_nomis_uppercase_code_dimension_uses_name(tmp_path, monkeypatch):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        [
            "DATE",
            "GEOGRAPHY_CODE",
            "GEOGRAPHY_NAME",
            "OBS_VALUE",
            "OBS_STATUS",
            "RECORD_COUNT",
            "RECORD_OFFSET",
        ]
    )
    writer.writerow(["2026-08", "K02000001", "United Kingdom", "001", "A", 1, 0])

    def handler(request):
        return (
            httpx.Response(200, json=test_nomis.definition())
            if request.url.path.endswith(".json")
            else httpx.Response(200, text=stream.getvalue())
        )

    store = Store(tmp_path)
    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = store.fetch(test_nomis.default_request(), client=client)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    data = Service(tmp_path)
    result = data.list_dimension_values("nomis:NM_162_1", "GEOGRAPHY_CODE", "United")
    assert result["items"] == [{"value": "K02000001", "label": "United Kingdom"}]
    provenance = data.get_provenance(snapshot["product_id"])
    assert provenance["source_acquisitions"][0]["snapshot_id"] == snapshot["product_id"]
    assert provenance["effective_source_request"] == snapshot["request"]


def test_derived_selection_provenance_reaches_national_acquisition(service):
    data, source_id = service
    selection = SharedDeprivation(data.store).fetch(
        Request("deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]})
    )
    provenance = data.get_provenance(selection["product_id"])
    assert provenance["product_request"] == selection["request"]
    assert provenance["product_request"]["params"]["local_authority_codes"] == ["E06000044"]
    assert len(provenance["source_acquisitions"]) == 1
    acquisition = provenance["source_acquisitions"][0]
    assert acquisition["snapshot_id"] == source_id
    assert acquisition["request"] == data.store.verify(source_id)["request"]
    assert provenance["effective_source_request"] == acquisition["request"]
    assert "local_authority_codes" not in provenance["effective_source_request"]["params"]


def test_provenance_resolves_multiple_derived_steps(service):
    data, source_id = service
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
    )
    selection = SharedDeprivation(data.store).fetch(request)

    metadata = json.loads((data.store.path(selection["product_id"]) / "metadata.json").read_bytes())
    metadata["derived_step"] = "Retained analytical copy"
    acquisition = Acquisition(
        "deprivation",
        selection["dataset_id"],
        selection["title"],
        data.store.replay(selection["product_id"]),
        metadata,
        selection["release"],
        selection["licence"],
        selection["citation"],
        selection["source_url"],
    )
    derived = data.store._publish(
        request,
        hashlib.sha256(canonical(request.to_dict())).hexdigest(),
        acquisition,
        [],
        kind="selection",
        parents=[selection["product_id"]],
    )
    provenance = data.get_provenance(derived["product_id"])
    assert provenance["parent_snapshot_ids"] == [selection["product_id"]]
    assert [item["snapshot_id"] for item in provenance["source_acquisitions"]] == [source_id]
    assert provenance["effective_source_request"] == data.store.verify(source_id)["request"]


def test_provenance_traversal_budget(service, monkeypatch):
    data, _ = service
    ids = [f"{number:064x}" for number in range(11)]
    manifests = {
        key: {"product_id": key, "parent_snapshot_ids": [ids[index + 1]]}
        for index, key in enumerate(ids[:-1])
    }
    monkeypatch.setattr(data, "_manifest", lambda key: manifests[key])
    with pytest.raises(RetrievalError, match="traversal budget"):
        data._source_acquisitions(manifests[ids[0]])
