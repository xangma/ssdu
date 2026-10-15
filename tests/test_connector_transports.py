"""Synthetic connector metadata stays cached-only through REST and MCP."""

import json
import socket
import sys
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from ssdu.api import create_app
from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.mcp_server import create_mcp_server
from ssdu.models import RetrievalError
from ssdu.service import Service


class MetadataService:
    def __init__(self):
        self.calls = []

    def list_provider_datasets(self, provider, query="", limit=20, offset=0):
        self.calls.append(("datasets", provider, query, limit, offset))
        return {
            "provider": provider,
            "items": [{"dataset_id": "invented-study", "title": "Invented community study"}],
            "offline": True,
        }

    def describe_provider_dataset(self, provider, dataset_id):
        if dataset_id == "missing":
            raise KeyError("/private/cache/missing")
        if dataset_id == "broken":
            raise RetrievalError("/private/cache/corrupt")
        self.calls.append(("describe", provider, dataset_id))
        return {
            "provider": provider,
            "dataset_id": dataset_id,
            "dimensions": [{"id": "age", "label": "Invented age band"}],
            "offline": True,
        }

    def list_provider_dimension_values(
        self, provider, dataset_id, dimension, search="", limit=20, offset=0
    ):
        self.calls.append(("values", provider, dataset_id, dimension, search, limit, offset))
        return {"items": [{"code": "01", "label": "Invented band"}], "offline": True}


@pytest.fixture
def no_provider_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Metadata transport attempted provider network access")

    monkeypatch.setattr(HttpClient, "request", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)


def test_provider_rest_mcp_metadata_parity_and_read_only_tools(no_provider_network):
    service = MetadataService()
    api = TestClient(create_app(service=service))
    cases = (
        (
            "/providers/nomis/datasets",
            "list_provider_datasets",
            {"provider": "nomis", "query": "community", "limit": 1, "offset": 2},
        ),
        (
            "/providers/nomis/datasets/invented-study",
            "describe_provider_dataset",
            {"provider": "nomis", "dataset_id": "invented-study"},
        ),
        (
            "/providers/nomis/datasets/invented-study/dimensions/age",
            "list_provider_dimension_values",
            {
                "provider": "nomis",
                "dataset_id": "invented-study",
                "dimension": "age",
                "search": "band",
                "limit": 1,
                "offset": 0,
            },
        ),
    )

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for path, name, args in cases:
                tool = tools[name]
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.open_world_hint is False
                assert tool.input_schema["additionalProperties"] is False
                assert "refresh" not in tool.input_schema["properties"]
                params = {
                    key: value
                    for key, value in args.items()
                    if key not in {"provider", "dataset_id", "dimension"}
                }
                response = api.get(path, params=params)
                assert response.status_code == 200
                result = await client.call_tool(name, args)
                assert not result.is_error
                assert result.structured_content == response.json()
                assert response.json()["offline"] is True

    anyio.run(exercise)


@pytest.mark.parametrize("params", [{"limit": 101}, {"offset": -1}, {"refresh": True}])
def test_provider_rest_rejects_bounds_and_network_policy(params, no_provider_network):
    service = MetadataService()
    api = TestClient(create_app(service=service))
    assert api.get("/providers/nomis/datasets", params=params).status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("dataset_id,status", [("missing", 404), ("broken", 409)])
def test_provider_metadata_errors_are_redacted(dataset_id, status, no_provider_network):
    service = MetadataService()
    api = TestClient(create_app(service=service))
    response = api.get(f"/providers/nomis/datasets/{dataset_id}")
    assert response.status_code == status
    assert "/private" not in response.text

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            result = await client.call_tool(
                "describe_provider_dataset", {"provider": "nomis", "dataset_id": dataset_id}
            )
            assert result.is_error
            assert "/private" not in result.content[0].text

    anyio.run(exercise)


@pytest.mark.parametrize("args", [{"limit": "20"}, {"offset": -1}, {"refresh": True}])
def test_provider_mcp_rejects_bounds_types_and_network_policy(args, no_provider_network):
    service = MetadataService()

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            result = await client.call_tool("list_provider_datasets", {"provider": "nomis", **args})
            assert result.is_error

    anyio.run(exercise)
    assert service.calls == []


@pytest.fixture
def cli_connector(monkeypatch):
    calls = []

    class InventedConnector:
        def __init__(self, provider, root="data", client=None):
            calls.append(("init", provider, root))

        def list_datasets(self, query="", **kwargs):
            calls.append(("datasets", query, kwargs))
            return {"items": [], "synthetic": True}

        def describe_dataset(self, dataset_id, **kwargs):
            calls.append(("describe", dataset_id, kwargs))
            return {"dataset_id": dataset_id, "synthetic": True}

        def list_dimension_values(self, dataset_id, dimension, **kwargs):
            calls.append(("values", dataset_id, dimension, kwargs))
            return {"items": [], "synthetic": True}

        def plan_fetch(self, request, **kwargs):
            calls.append(("plan", request.to_dict(), kwargs))
            return {"request": request.to_dict(), "synthetic": True}

    monkeypatch.setitem(
        sys.modules, "ssdu.connectors", SimpleNamespace(Connector=InventedConnector)
    )
    return calls


@pytest.mark.parametrize(
    "command,expected",
    [
        (
            ["datasets", "nomis", "--query", "community", "--limit", "3", "--offset", "2"],
            ("datasets", "community", {"limit": 3, "offset": 2}),
        ),
        (["describe", "statswales", "invented-study"], ("describe", "invented-study", {})),
        (
            ["values", "nomis", "invented-study", "age", "--search", "band", "--limit", "1"],
            ("values", "invented-study", "age", {"search": "band", "limit": 1, "offset": 0}),
        ),
    ],
)
@pytest.mark.parametrize("flag", [None, "--offline", "--refresh"])
def test_connector_cli_forwards_explicit_metadata_policy(
    command, expected, flag, cli_connector, tmp_path, capsys
):
    args = ["--store", str(tmp_path), "connector", *command]
    if flag:
        args.append(flag)
    assert main(args) == 0
    assert cli_connector[0] == ("init", command[1], str(tmp_path))
    policy = {"offline": flag == "--offline", "refresh": flag == "--refresh"}
    assert cli_connector[1] == (*expected[:-1], {**expected[-1], **policy})
    assert json.loads(capsys.readouterr().out)["synthetic"] is True


def test_connector_cli_plan_request_and_policy(cli_connector, tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"provider": "nomis", "dataset_id": "NM_999_1"}))
    assert main(["--store", str(tmp_path), "connector", "plan", str(request), "--offline"]) == 0
    assert cli_connector[0] == ("init", "nomis", str(tmp_path))
    assert cli_connector[1][0] == "plan"
    assert cli_connector[1][1]["dataset_id"] == "NM_999_1"
    assert cli_connector[1][2] == {"offline": True, "refresh": False}
    assert json.loads(capsys.readouterr().out)["request"]["provider"] == "nomis"


def test_connector_cli_limits_request_body_and_excludes_combined_policy(
    cli_connector, tmp_path, capsys
):
    request = tmp_path / "request.json"
    request.write_bytes(b" " * 64_001)
    assert main(["connector", "plan", str(request)]) == 1
    assert "64 KB" in capsys.readouterr().err
    assert cli_connector == []
    with pytest.raises(SystemExit):
        main(["connector", "datasets", "nomis", "--offline", "--refresh"])


def test_actual_service_connector_methods_force_offline_metadata(
    cli_connector, tmp_path, no_provider_network
):
    service = Service(tmp_path, index_store=False)
    service.list_provider_datasets("nomis", query="invented", limit=2, offset=1)
    service.describe_provider_dataset("statswales", "invented-study")
    service.list_provider_dimension_values("nomis", "invented-study", "age", limit=1)
    operations = [call for call in cli_connector if call[0] != "init"]
    assert [call[0] for call in operations] == ["datasets", "describe", "values"]
    for call in operations:
        assert call[-1]["offline"] is True
        assert not call[-1].get("refresh", False)


def test_prepared_provider_metadata_replays_through_actual_rest_and_mcp(tmp_path, monkeypatch):
    from ssdu.connectors import Connector

    dataset_id = "NM_999_1"
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
                                    {"value": "001", "description": {"value": "Invented services"}}
                                ],
                            }
                        }
                    }
                },
            )
        assert request.url.path.endswith("/def.sdmx.json")
        return httpx.Response(
            200,
            json={
                "structure": {
                    "keyfamilies": {
                        "keyfamily": [
                            {
                                "id": dataset_id,
                                "name": {"value": "Invented economic activity"},
                                "components": {"dimension": [{"conceptref": "INDUSTRY"}]},
                            }
                        ]
                    }
                }
            },
        )

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        connector = Connector("nomis", root=tmp_path, client=client)
        expected = [
            connector.list_datasets(),
            connector.describe_dataset(dataset_id),
            connector.list_dimension_values(dataset_id, "industry"),
        ]
    assert len(calls) == 3

    def fail(*args, **kwargs):
        pytest.fail("Prepared provider metadata attempted network access")

    monkeypatch.setattr(HttpClient, "request", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    service = Service(tmp_path, index_store=False)
    api = TestClient(create_app(service=service))
    prefix = "/providers/nomis/datasets"
    paths = [prefix, f"{prefix}/{dataset_id}", f"{prefix}/{dataset_id}/dimensions/industry"]
    for path, document in zip(paths, expected, strict=True):
        response = api.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == document

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            tools = [
                ("list_provider_datasets", {"provider": "nomis"}),
                ("describe_provider_dataset", {"provider": "nomis", "dataset_id": dataset_id}),
                (
                    "list_provider_dimension_values",
                    {"provider": "nomis", "dataset_id": dataset_id, "dimension": "industry"},
                ),
            ]
            for (name, args), document in zip(tools, expected, strict=True):
                result = await client.call_tool(name, args)
                assert not result.is_error
                assert result.structured_content == document

    anyio.run(exercise)
