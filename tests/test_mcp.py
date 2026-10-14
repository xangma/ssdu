import json
import sys
from pathlib import Path

import anyio
import httpx2
import pytest
import test_api
import test_deprivation
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from test_api import SNAPSHOT, FakeService

from ssdu.api import create_app
from ssdu.http import HttpClient
from ssdu.mcp_server import create_mcp_server

local_service = test_api.local_service
iod_csv = test_deprivation.iod_csv


def test_mcp_protocol_tools_resources_and_bounded_input(monkeypatch):
    def no_http(*args, **kwargs):
        pytest.fail("MCP read attempted provider access")

    monkeypatch.setattr(HttpClient, "request", no_http)
    service = FakeService()

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "search_datasets",
                "describe_dataset",
                "list_dimension_values",
                "preview_data",
                "query_data",
                "get_provenance",
            }
            for tool in listing.tools:
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.idempotent_hint is True
                assert tool.input_schema["additionalProperties"] is False
                assert tool.output_schema is not None
            search = await client.call_tool("search_datasets", {"query": "deprivation", "limit": 5})
            assert not search.is_error
            assert search.structured_content["items"][0]["dataset_id"] == "deprivation:iod2025"
            query = {"count_only": True, "filters": [{"column": "decile", "value": 1}]}
            count = await client.call_tool("query_data", {"snapshot_id": SNAPSHOT, "query": query})
            assert not count.is_error
            assert count.structured_content["total_rows"] == 15
            assert count.structured_content["provenance"]["snapshot_id"] == SNAPSHOT
            resource = await client.read_resource("ssdu://datasets/deprivation:iod2025")
            assert json.loads(resource.contents[0].text)["dataset_id"] == "deprivation:iod2025"
            provenance = await client.read_resource(f"ssdu://snapshots/{SNAPSHOT}/provenance")
            assert json.loads(provenance.contents[0].text)["snapshot_id"] == SNAPSHOT
            for name, args in (
                ("search_datasets", {"limit": 101}),
                ("search_datasets", {"limit": "20"}),
                ("search_datasets", {"sql": "SELECT 1"}),
                ("query_data", {"snapshot_id": SNAPSHOT, "query": {"limit": 101}}),
                ("query_data", {"snapshot_id": SNAPSHOT, "query": {"sql": "SELECT 1"}}),
                ("get_provenance", {"snapshot_id": "../../private/file"}),
                ("describe_dataset", {"dataset_id": "missing"}),
                ("describe_dataset", {"dataset_id": "broken"}),
                ("describe_dataset", {"dataset_id": "crash"}),
            ):
                result = await client.call_tool(name, args)
                assert result.is_error
                assert "/private" not in result.content[0].text

    anyio.run(exercise)


def test_actual_stdio_protocol():
    # Real subprocess and JSON-RPC framing, with the same injected local-only service.
    tests_path = str(Path(__file__).parent)
    source = (
        "import sys; "
        f"sys.path.insert(0, {tests_path!r}); "
        "from test_api import FakeService; "
        "from ssdu.mcp_server import create_mcp_server; "
        "create_mcp_server(service=FakeService()).run()"
    )

    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", source])
        async with Client(parameters, read_timeout_seconds=10) as client:
            assert len((await client.list_tools()).tools) == 6
            result = await client.call_tool(
                "query_data", {"snapshot_id": SNAPSHOT, "query": {"count_only": True}}
            )
            assert not result.is_error
            assert result.structured_content["total_rows"] == 15

    anyio.run(exercise)


def test_streamable_http_protocol():
    async def exercise():
        app = create_mcp_server(service=FakeService()).streamable_http_app(
            stateless_http=True, json_response=True, max_request_body_size=64_000
        )
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8001"
            ) as http,
        ):
            transport = streamable_http_client("http://127.0.0.1:8001/mcp", http_client=http)
            async with Client(transport, read_timeout_seconds=10) as client:
                result = await client.call_tool(
                    "query_data", {"snapshot_id": SNAPSHOT, "query": {"count_only": True}}
                )
                assert not result.is_error
                assert result.structured_content["total_rows"] == 15

    anyio.run(exercise)


def test_actual_service_rest_mcp_parity_offline(local_service):
    service, manifest = local_service
    snapshot_id = manifest["product_id"]
    api = TestClient(create_app(service=service))
    query = {
        "columns": ["lsoa_code", "imd_decile"],
        "filters": [
            {"column": "local_authority_code", "value": "E06000044"},
            {"column": "imd_decile", "value": 5},
        ],
        "limit": 100,
    }
    rest_result = api.post(f"/snapshots/{snapshot_id}/query", json=query).json()

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            search = await client.call_tool("search_datasets", {"query": "deprivation"})
            key = search.structured_content["items"][0]["dataset_id"]
            metadata = await client.call_tool("describe_dataset", {"dataset_id": key})
            assert metadata.structured_content == api.get(f"/datasets/{key}").json()
            dimensions = await client.call_tool(
                "list_dimension_values",
                {"dataset_id": key, "dimension": "local_authority_code", "search": "Portsmouth"},
            )
            assert dimensions.structured_content["items"] == [
                {"value": "E06000044", "label": "Portsmouth"}
            ]
            for name in ("query_data", "preview_data"):
                result = await client.call_tool(name, {"snapshot_id": snapshot_id, "query": query})
                assert not result.is_error
                assert result.structured_content == rest_result
            count = await client.call_tool(
                "query_data", {"snapshot_id": snapshot_id, "query": {**query, "count_only": True}}
            )
            assert count.structured_content["rows"] == [{"count": 2}]
            provenance = await client.call_tool("get_provenance", {"snapshot_id": snapshot_id})
            resource = await client.read_resource(f"ssdu://snapshots/{snapshot_id}/provenance")
            assert json.loads(resource.contents[0].text) == provenance.structured_content
            assert provenance.structured_content == rest_result["provenance"]
            assert (
                provenance.structured_content["metadata_version"]
                == metadata.structured_content["metadata_version"]
            )
        # Exercise the real CLI module entry in a subprocess, guarding provider access there too.
        source = (
            "from ssdu.http import HttpClient; "
            "HttpClient.request = lambda *a, **k: (_ for _ in ()).throw("
            "RuntimeError('Offline MCP attempted provider access')); "
            "from ssdu.mcp_server import main; "
            f"main(['--store', {str(service.store.root)!r}])"
        )
        parameters = StdioServerParameters(command=sys.executable, args=["-c", source])
        async with Client(parameters, read_timeout_seconds=10) as client:
            result = await client.call_tool(
                "query_data", {"snapshot_id": snapshot_id, "query": query}
            )
            assert not result.is_error
            assert result.structured_content == rest_result

    anyio.run(exercise)
