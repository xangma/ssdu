"""Independent expected cells exercise each declared family through cached adapters."""

import ast
import csv
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pyarrow as pa
import pytest

from ssdu import Store
from ssdu.adapters import deprivation
from ssdu.conformance import conformance_coverage, get_conformance_families
from ssdu.http import HttpClient
from ssdu.models import Request
from ssdu.rate_limit import RateLimiter

FIXTURES = Path(__file__).parent / "fixtures/conformance"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_bytes())
FAMILY_IDS = tuple(MANIFEST["families"])


def payload_for(family_id):
    family = MANIFEST["families"][family_id]
    path = FIXTURES / family["payload"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == MANIFEST["files"][path.name]
    return family, json.loads(path.read_bytes())


class FamilyResponses:
    """Closed, synthetic transport; exact provider endpoints are retained in snapshots."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, request):
        self.calls.append(str(request.url))
        response = self.response(request)
        response.headers["x-ssdu-fixture"] = "independently_invented_conformance"
        return response

    def response(self, request):
        data = self.payload
        native_id = data["request"]["dataset_id"]
        path = request.url.path
        if data["format"] == "nomis_csv" and request.url.host == "www.nomisweb.co.uk":
            if path == f"/api/v01/dataset/{native_id}/def.sdmx.json":
                return httpx.Response(200, json=data["definition"])
            if path == f"/api/v01/dataset/{native_id}.data.csv":
                offset = int(request.url.params["recordoffset"])
                size = int(request.url.params["recordlimit"])
                columns = list(dict.fromkeys(key for row in data["rows"] for key in row))
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=columns)
                writer.writeheader()
                writer.writerows(data["rows"][offset : offset + size])
                return httpx.Response(200, text=output.getvalue())
        if data["format"] == "statswales_v2" and request.url.host == "api.stats.gov.wales":
            base = f"/v2/{native_id}"
            if path == base:
                return httpx.Response(200, json=data["metadata"])
            if path == base + "/filters":
                return httpx.Response(200, json=data["dimensions"])
            if request.method == "POST" and path == base + "/data":
                assert json.loads(request.content)["options"] == {"data_value_type": "formatted"}
                return httpx.Response(200, json={"filterId": "synthetic001"})
            if path == base + "/query/synthetic001":
                return httpx.Response(
                    200,
                    json={
                        "id": "synthetic001",
                        "datasetId": native_id,
                        "revisionId": data["metadata"]["published_revision"]["id"],
                        "totalLines": len(data["rows"]),
                    },
                )
            if path == base + "/data/synthetic001":
                page = int(request.url.params["page_number"])
                size = int(request.url.params["page_size"])
                return httpx.Response(200, json=data["rows"][(page - 1) * size : page * size])
        if data["format"] == "deprivation_csv" and str(request.url) == deprivation.SOURCE_URL:
            return httpx.Response(200, text=data["csv"], headers={"content-type": "text/csv"})
        raise AssertionError(f"Unregistered synthetic endpoint: {request.method} {request.url}")


def assert_expected(dataset, expected):
    table = dataset.to_arrow()
    assert dataset.contract["quality"]["source_rows"] == expected["source_rows"]
    assert table.num_rows == expected["observation_rows"]
    for column, values in expected.get("columns", {}).items():
        assert table[column].to_pylist() == values, column
    for measure, columns in expected.get("measure_columns", {}).items():
        selected = [row for row in table.to_pylist() if row["measure"] == measure]
        for column, values in columns.items():
            assert [row[column] for row in selected] == values, (measure, column)
    variables = {item["id"]: item for item in dataset.contract["variables"]}
    for measure, fields in expected.get("variables", {}).items():
        for key, value in fields.items():
            assert variables[measure][key] == value
    diagnostics = {item["code"] for item in dataset.contract["quality"]["diagnostics"]}
    assert diagnostics == set(expected["diagnostics"])


@pytest.mark.parametrize("family_id", FAMILY_IDS)
def test_registered_family_contract(family_id, tmp_path, monkeypatch):
    family, payload = payload_for(family_id)
    request = Request(**payload["request"])
    responses = FamilyResponses(payload)
    limiter = RateLimiter(tmp_path / "rate.sqlite3", interval_seconds=0)
    store = Store(tmp_path / "source")
    reservations = []
    acquire = limiter.acquire

    def reserve(host, **kwargs):
        acquire(host, **kwargs)
        reservations.append(host)

    monkeypatch.setattr(limiter, "acquire", reserve)

    # A mock transport performs all acquisition, while any accidental socket fails.
    def forbidden(*args, **kwargs):
        pytest.fail("Frozen conformance tried to contact a provider")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    with HttpClient(
        transport=httpx.MockTransport(responses), retries=0, rate_limiter=limiter
    ) as client:
        dataset = store.fetch_dataset(request, client=client)
        assert client.attempts == len(responses.calls) == len(reservations) > 0
    assert_expected(dataset, family["expected"])
    contract = dataset.contract
    assert contract["contract_version"] == "1.0.0"
    assert contract["profile"] == "observations"
    assert contract["source"]["effective_request"] == request.to_dict()
    if contract["source"]["parent_snapshot_ids"]:
        assert any(
            store.open_dataset(parent).contract["source"]["raw_inputs"]
            for parent in contract["source"]["parent_snapshot_ids"]
        )
    else:
        assert contract["source"]["raw_inputs"]
    assert pa.types.is_float64(dataset.to_arrow().schema.field("value").type)
    assert dataset.to_arrow("source").num_rows == family["expected"]["source_rows"]
    if request.provider == "deprivation":
        assert contract["source"]["release"]["lsoa_geography"] == "2021"
        assert contract["source"]["release"]["lad_geography"] == "2024"
        assert not any(dim["kind"] == "time" for dim in contract["dimensions"])
    with patch.object(HttpClient, "request", forbidden):
        cached = store.fetch_dataset(request)
        offline = Store(store.root).fetch_dataset(request, offline=True)
        reopened = Store(store.root).open_dataset(dataset.snapshot_id)
    assert dataset.snapshot_id == cached.snapshot_id == offline.snapshot_id == reopened.snapshot_id
    assert contract == reopened.contract
    for table_name in dataset.table_names:
        assert dataset.to_arrow(table_name).equals(reopened.to_arrow(table_name))


def test_family_manifest_binds_all_invented_inputs_and_expected_values():
    assert "no published data cells" in conformance_coverage()["fixture_policy"]
    assert "independently invented" in MANIFEST["description"].lower()
    assert set(MANIFEST["files"]) == {item["payload"] for item in MANIFEST["families"].values()}
    declared = {item.family_id: item for item in get_conformance_families()}
    for family_id in FAMILY_IDS:
        fixture, payload = payload_for(family_id)
        assert fixture["evidence_kind"] == declared[family_id].evidence_kind == "synthetic_boundary"
        assert fixture["provider"] == declared[family_id].provider == payload["request"]["provider"]
        assert fixture["references"] == list(declared[family_id].references)
        assert "no published records" in fixture["provenance"]["statement"]
        assert fixture["expected"]["source_rows"] > 0
        assert fixture["expected"].get("columns") or fixture["expected"].get("measure_columns")
        assert all(reference.startswith("https://") for reference in fixture["references"])


def test_every_declared_family_has_a_semantic_test():
    root = Path(__file__).parents[1]
    ids = [item.family_id for item in get_conformance_families()]
    assert len(ids) == len(set(ids))
    for family in get_conformance_families():
        path, name = family.test_node.split("::", 1)
        tree = ast.parse((root / path).read_text())
        function_name = name.split("[", 1)[0]
        assert any(
            isinstance(node, ast.FunctionDef) and node.name == function_name
            for node in ast.walk(tree)
        )
        if path == "tests/test_conformance_families.py":
            assert family.family_id in FAMILY_IDS
            assert name == f"test_registered_family_contract[{family.family_id}]"


def test_family_coverage_is_detached_and_has_three_shapes_per_existing_provider():
    first = conformance_coverage()
    first["families"][0]["scope"] = "changed"
    assert conformance_coverage()["families"][0]["scope"] != "changed"
    for provider in ("nomis", "statswales", "deprivation", "mhclg"):
        families = get_conformance_families(provider)
        assert len(families) >= 3
        assert len({item.scope for item in families}) == len(families)
