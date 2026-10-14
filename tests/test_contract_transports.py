"""Real REST and MCP clients read the same verified observations and local records."""

import json
import socket
import sys
from decimal import Decimal

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from test_adapter_conformance import example_module

from ssdu.api import create_app
from ssdu.contracts import DatasetQuerySpec
from ssdu.http import HttpClient
from ssdu.local_import import import_local
from ssdu.mcp_server import create_mcp_server
from ssdu.service import Service
from ssdu.store import Store


@pytest.fixture
def contract_store(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    example = example_module()
    with example.no_network(), example.fixture_client("nomis") as client:
        observations = store.fetch_dataset(example.request_for("nomis"), client=client)
    source = tmp_path / "invented-local.csv"
    source.write_text("code,label,rate\n001,Invented A,1.25\n002,Invented B,\n")
    manifest = import_local(
        store,
        source,
        {
            "dataset_id": "invented-local",
            "title": "Invented local records",
            "primary_key": ["code"],
            "column_types": {"rate": "float64"},
            "columns": {
                "code": {"role": "identifier", "nullable": False},
                "rate": {"unit": "percent"},
            },
        },
    )

    def no_network(*args, **kwargs):
        pytest.fail("Contract read attempted a network request")

    monkeypatch.setattr(HttpClient, "request", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    return Service(store.root), {
        "observations": observations.snapshot_id,
        "records": manifest["product_id"],
    }


@pytest.mark.parametrize("profile", ["observations", "records"])
def test_contract_service_rest_mcp_and_resource_parity_offline(contract_store, profile):
    service, snapshots = contract_store
    snapshot_id = snapshots[profile]
    api = TestClient(create_app(service=service))
    expected_descriptor = service.describe_snapshot(snapshot_id)
    assert expected_descriptor["profile"] == expected_descriptor["default_table"] == profile
    response = api.get(f"/snapshots/{snapshot_id}/dataset")
    assert response.status_code == 200
    assert response.json() == expected_descriptor
    for dimension in expected_descriptor["descriptor"]["dimensions"]:
        assert "codes" not in dimension
        assert dimension["codes_omitted"] is True

    bodies = [{}, {"table": profile, "limit": 1}, {"count_only": True}, {"table": "source"}]
    if profile == "observations":
        bodies.append(
            {
                "columns": ["value", "value_raw", "status"],
                "filters": [{"column": "status", "value": "S"}],
            }
        )
    else:
        bodies.append(
            {"columns": ["code", "rate"], "filters": [{"column": "code", "value": "001"}]}
        )

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            described = await client.call_tool("describe_snapshot", {"snapshot_id": snapshot_id})
            assert not described.is_error
            assert described.structured_content == expected_descriptor
            resource = await client.read_resource(f"ssdu://snapshots/{snapshot_id}/dataset")
            assert json.loads(resource.contents[0].text) == expected_descriptor
            for body in bodies:
                expected = service.query_dataset(snapshot_id, DatasetQuerySpec.model_validate(body))
                assert (
                    expected["snapshot_id"] == expected["provenance"]["snapshot_id"] == snapshot_id
                )
                assert expected["profile"] == profile
                assert expected["table"] == (body.get("table") or profile)
                assert expected["quality"] == expected_descriptor["descriptor"]["quality"]
                result = api.post(f"/snapshots/{snapshot_id}/dataset/query", json=body)
                assert result.status_code == 200
                assert result.json() == expected
                result = await client.call_tool(
                    "query_dataset", {"snapshot_id": snapshot_id, "query": body}
                )
                assert not result.is_error
                assert result.structured_content == expected
            if profile == "observations":
                assert expected["rows"] == [{"value": None, "value_raw": "..", "status": "S"}]
                assert expected["units"]["value_column"] == "unit"
            else:
                assert expected["rows"] == [{"code": "001", "rate": 1.25}]
                assert expected["units"]["columns"] == {"rate": "percent"}
            dimensions = [item["id"] for item in expected_descriptor["descriptor"]["dimensions"]]
            if profile == "records":
                dimensions = ["code"]  # Explicit identifier role, without inferred geography.
            for name in dimensions:
                kwargs = {"search": "", "limit": 1, "offset": 0}
                expected = service.list_snapshot_dimension_values(snapshot_id, name, **kwargs)
                response = api.get(
                    f"/snapshots/{snapshot_id}/dataset/dimensions/{name}", params=kwargs
                )
                assert response.status_code == 200
                assert response.json() == expected
                result = await client.call_tool(
                    "list_snapshot_dimension_values",
                    {"snapshot_id": snapshot_id, "dimension": name, **kwargs},
                )
                assert not result.is_error
                assert result.structured_content == expected
                if profile == "records":
                    assert expected["items"] == [{"value": "001", "label": "001"}]
                    assert expected["truncated"] is True and expected["next_offset"] == 1
                    args = {"snapshot_id": snapshot_id, "dimension": name, "limit": 1, "offset": 1}
                    next_page = await client.call_tool("list_snapshot_dimension_values", args)
                    assert not next_page.is_error
                    assert next_page.structured_content["items"] == [
                        {"value": "002", "label": "002"}
                    ]
                    assert next_page.structured_content["next_offset"] is None

    anyio.run(exercise)


def test_contract_query_actual_stdio_is_offline(contract_store):
    service, snapshots = contract_store
    body = {"columns": ["code", "rate"], "filters": [{"column": "code", "value": "002"}]}
    snapshot_id = snapshots["records"]
    expected = service.query_dataset(snapshot_id, DatasetQuerySpec.model_validate(body))
    source = (
        "from ssdu.http import HttpClient; "
        "HttpClient.request = lambda *a, **k: (_ for _ in ()).throw("
        "RuntimeError('Offline contract read attempted provider access')); "
        "import socket; "
        "socket.socket.connect = socket.socket.connect_ex = socket.create_connection = "
        "lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Network disabled')); "
        "from ssdu.mcp_server import main; "
        f"main(['--store', {str(service.store.root)!r}])"
    )

    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", source])
        async with Client(parameters, read_timeout_seconds=10) as client:
            result = await client.call_tool(
                "query_dataset", {"snapshot_id": snapshot_id, "query": body}
            )
            assert not result.is_error
            assert result.structured_content == expected
            assert result.structured_content["rows"] == [{"code": "002", "rate": None}]

    anyio.run(exercise)


def test_contract_query_rejects_nonexistent_tables_columns_and_dimensions(contract_store):
    service, snapshots = contract_store
    api = TestClient(create_app(service=service))
    for profile, snapshot_id in snapshots.items():
        unavailable = "records" if profile == "observations" else "observations"
        for body in (
            {"table": unavailable},
            {"columns": ["missing"]},
            {"filters": [{"column": "missing", "value": 1}]},
        ):
            response = api.post(f"/snapshots/{snapshot_id}/dataset/query", json=body)
            assert response.status_code == 400
            assert "/private" not in response.text
        assert api.get(f"/snapshots/{snapshot_id}/dataset/dimensions/missing").status_code == 400


def test_native_parquet_precision_survives_rest_and_mcp(tmp_path, monkeypatch):
    """Transport serialization must retain precision beyond Python datetime and DuckDB decimal limits."""
    source = tmp_path / "invented-native.parquet"
    number = "123456789012345678901234567890.123456789012345678901234567890"
    table = pa.table(
        {
            "code": ["001"],
            "amount": pa.array([Decimal(number)], type=pa.decimal256(60, 30)),
            "instant": pa.array([1_234_567_890_123_456_789], type=pa.timestamp("ns", tz="UTC")),
            "clock": pa.array([1_234_567_890_123], type=pa.time64("ns")),
            "unsigned": pa.array([2**64 - 1], type=pa.uint64()),
        }
    )
    pq.write_table(table, source)
    store = Store(tmp_path / "store")
    manifest = import_local(
        store,
        source,
        {"dataset_id": "invented-native", "title": "Invented exact native types"},
    )

    def no_network(*args, **kwargs):
        pytest.fail("Native-type contract read attempted a network request")

    monkeypatch.setattr(HttpClient, "request", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    service = Service(store.root)
    snapshot_id = manifest["product_id"]
    expected = service.query_dataset(snapshot_id, DatasetQuerySpec())
    assert expected["rows"] == [
        {
            "code": "001",
            "amount": number,
            "instant": "2009-02-13T23:31:30.123456789+00:00",
            "clock": "00:20:34.567890123",
            "unsigned": 2**64 - 1,
        }
    ]
    response = TestClient(create_app(service=service)).post(
        f"/snapshots/{snapshot_id}/dataset/query", json={}
    )
    assert response.status_code == 200
    assert response.json() == expected

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            result = await client.call_tool(
                "query_dataset", {"snapshot_id": snapshot_id, "query": {}}
            )
            assert not result.is_error
            assert result.structured_content == expected

    anyio.run(exercise)
