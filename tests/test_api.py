import pytest
import test_deprivation
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_deprivation import client_for

from ssdu.api import create_app
from ssdu.contracts import Filter, QuerySpec
from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.service import Service
from ssdu.shared import SharedDeprivation
from ssdu.store import Store

SNAPSHOT = "a" * 64
iod_csv = test_deprivation.iod_csv


class FakeService:
    def __init__(self):
        self.calls = []

    def search_datasets(self, query="", provider=None, limit=20, offset=0):
        self.calls.append(("search", query, provider, limit, offset))
        return {"items": [{"dataset_id": "deprivation:iod2025", "title": "English deprivation"}]}

    def describe_dataset(self, dataset_id):
        if dataset_id == "missing":
            raise KeyError("/private/internal/path")
        if dataset_id == "broken":
            raise RetrievalError("hash mismatch: /private/internal/path")
        if dataset_id == "crash":
            raise RuntimeError("/private/internal/path")
        return {"dataset_id": dataset_id, "definitions": "Provider text is source material."}

    def list_dimension_values(self, dataset_id, dimension, search="", limit=20, offset=0):
        self.calls.append(("dimensions", dataset_id, dimension, search, limit, offset))
        return {"items": [{"code": "E06000044", "label": "Portsmouth"}]}

    def preview_data(self, snapshot_id, query):
        self.calls.append(("preview", snapshot_id, query))
        return self.query_data(snapshot_id, query)

    def query_data(self, snapshot_id, query):
        self.calls.append(("query", snapshot_id, query))
        return {
            "rows": [] if query.count_only else [{"area": "Portsmouth", "decile": 1}],
            "total_rows": 15,
            "truncated": not query.count_only,
            "next_offset": 1 if not query.count_only else None,
            "provenance": self.get_provenance(snapshot_id),
            "effective_query": query.model_dump(),
        }

    def get_provenance(self, snapshot_id):
        return {"snapshot_id": snapshot_id, "source_url": "https://example.gov.uk/deprivation"}


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def api(service, monkeypatch):
    def no_http(*args, **kwargs):
        pytest.fail("Read-only API attempted provider access")

    monkeypatch.setattr(HttpClient, "request", no_http)
    return TestClient(create_app(service=service), raise_server_exceptions=False)


def test_rest_reads_and_openapi(api, service):
    assert api.get("/health").json()["mode"] == "local-read-only"
    assert api.get("/datasets?query=deprivation&provider=deprivation&limit=5").status_code == 200
    assert service.calls[-1] == ("search", "deprivation", "deprivation", 5, 0)
    assert api.get("/datasets/deprivation:iod2025").json()["dataset_id"] == "deprivation:iod2025"
    assert (
        api.get("/datasets/deprivation:iod2025/dimensions/area?search=Ports").json()["items"][0][
            "label"
        ]
        == "Portsmouth"
    )
    body = {"columns": ["area"], "filters": [{"column": "decile", "op": "eq", "value": 1}]}
    result = api.post(f"/snapshots/{SNAPSHOT}/query", json=body)
    assert result.status_code == 200
    assert result.json()["rows"][0]["area"] == "Portsmouth"
    assert result.json()["provenance"]["snapshot_id"] == SNAPSHOT
    assert result.json()["effective_query"]["columns"] == ["area"]
    assert api.post(f"/snapshots/{SNAPSHOT}/preview", json={}).status_code == 200
    count = api.post(f"/snapshots/{SNAPSHOT}/query", json={"count_only": True}).json()
    assert count["total_rows"] == 15 and count["rows"] == []
    assert api.get(f"/snapshots/{SNAPSHOT}/provenance").json()["snapshot_id"] == SNAPSHOT
    schema = api.get("/openapi.json").json()
    assert schema["components"]["schemas"]["QuerySpec"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"sql": "SELECT * FROM '/private/file'"},
        {"limit": 101},
        {"limit": "20"},
        {"count_only": "yes"},
        {"filters": [{"column": "decile", "op": "in", "value": 1}]},
        {"filters": [{"column": "decile", "op": "eq", "value": [1]}]},
        {"filters": [{"column": "decile", "op": "neq", "value": 1}]},
        {"columns": []},
    ],
)
def test_rest_rejects_invalid_queries(api, body):
    response = api.post(f"/snapshots/{SNAPSHOT}/query", json=body)
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]
    assert "/private/file" not in response.text


def test_rest_rejects_bad_ids_paging_unknown_args(api):
    assert api.get("/datasets?limit=101").status_code == 422
    assert api.get("/datasets?unknown=true").status_code == 422
    assert api.get("/datasets/foo/dimensions/area?unknown=true").status_code == 422
    assert api.get("/snapshots/nope/provenance").status_code == 422


@pytest.mark.parametrize("dataset,status", [("missing", 404), ("broken", 409), ("crash", 500)])
def test_rest_sanitizes_service_errors(api, dataset, status):
    response = api.get(f"/datasets/{dataset}")
    assert response.status_code == status
    assert "/private" not in response.text


def test_query_contract_limits():
    for args in ({"value": []}, {"value": [1] * 101}, {"value": float("inf")}, {"value": {}}):
        with pytest.raises(ValidationError):
            Filter(column="x", op="in", **args)
    with pytest.raises(ValidationError):
        QuerySpec(filters=[{"column": "x", "value": 1}] * 21)


def test_rest_caps_oversized_outputs(api, service):
    service.describe_dataset = lambda _: {"notes": "x" * 256_001}
    # Routes retain the injected service object and resolve the method on every request.
    assert api.get("/datasets/large").status_code == 400


def test_rest_rejects_large_body_before_json_parsing(api):
    response = api.post(
        f"/snapshots/{SNAPSHOT}/query",
        content=b"x" * 64_001,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


@pytest.fixture
def local_service(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path)
    with client_for(iod_csv) as client:
        manifest = SharedDeprivation(store).source(client=client)

    def no_http(*args, **kwargs):
        pytest.fail("A prepared local service attempted provider access")

    monkeypatch.setattr(HttpClient, "request", no_http)
    return Service(tmp_path), manifest


def test_actual_service_rest_offline(local_service):
    service, manifest = local_service
    snapshot_id = manifest["product_id"]
    api = TestClient(create_app(service=service))
    search = api.get("/datasets?query=deprivation").json()
    key = search["items"][0]["dataset_id"]
    metadata = api.get(f"/datasets/{key}").json()
    assert metadata["availability"]["materialised"]
    assert metadata["snapshots"][0]["snapshot_id"] == snapshot_id
    values = api.get(f"/datasets/{key}/dimensions/local_authority_code?search=Portsmouth").json()
    assert values["items"] == [{"value": "E06000044", "label": "Portsmouth"}]
    query = {
        "columns": ["lsoa_code", "imd_decile"],
        "filters": [
            {"column": "local_authority_code", "value": "E06000044"},
            {"column": "imd_decile", "value": 5},
        ],
        "limit": 100,
    }
    result = api.post(f"/snapshots/{snapshot_id}/query", json=query).json()
    assert result == service.query_data(snapshot_id, QuerySpec.model_validate(query))
    assert result["rows"] == [
        {"lsoa_code": "E01017013", "imd_decile": 5},
        {"lsoa_code": "E01017014", "imd_decile": 5},
    ]
    assert api.post(f"/snapshots/{snapshot_id}/preview", json=query).json() == result
    query["count_only"] = True
    count = api.post(f"/snapshots/{snapshot_id}/query", json=query).json()
    assert count["rows"] == [{"count": 2}]
    provenance = api.get(f"/snapshots/{snapshot_id}/provenance").json()
    assert provenance == result["provenance"]
    assert provenance["snapshot_id"] == snapshot_id
    assert provenance["metadata_version"] == metadata["metadata_version"]
