"""MCP retrieval controls have strict inputs and truthful mutation annotations."""

import sys
from pathlib import Path

import anyio
import pytest
import test_collections
import test_retrieval_api
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters

from ssdu.api import create_app
from ssdu.mcp_server import create_mcp_server, main
from ssdu.models import RetrievalError
from ssdu.service import Service

PLAN_ID = test_retrieval_api.PLAN_ID
JOB_ID = test_retrieval_api.JOB_ID
SPEC = test_retrieval_api.SPEC
CONTROL_TOOLS = test_retrieval_api.CONTROL_TOOLS
retrieval_service = test_retrieval_api.retrieval_service
collection_store = test_collections.collection_store


def test_mcp_controls_require_opt_in(retrieval_service):
    async def exercise():
        async with Client(create_mcp_server(service=retrieval_service)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert not names.intersection(CONTROL_TOOLS)
            assert len(names) == 9

    anyio.run(exercise)
    assert retrieval_service.calls == []


def test_actual_service_completed_job_is_visible_to_rest_and_mcp(collection_store):
    store, snapshot = collection_store
    service = Service(store.root)
    api = TestClient(create_app(service=service, enable_jobs=True))
    spec = {
        "name": "completed-offline-collection",
        "entries": {"observations": {"snapshot_id": snapshot["product_id"]}},
    }
    planned = api.post("/retrieval/plans", json={"spec": spec, "offline": True})
    assert planned.status_code == 201, planned.text
    queued = api.post(
        "/retrieval/jobs",
        json={"plan_id": planned.json()["plan_id"], "idempotency_key": "complete-offline"},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "queued"
    job_id = queued.json()["job_id"]
    # Execution is an explicit Python worker call, outside either transport.
    completed = service.run_job(job_id)
    assert completed["status"] == "succeeded", completed
    assert completed["result"]["collection"] == spec["name"]
    assert api.get(f"/retrieval/jobs/{job_id}").json() == completed

    async def exercise():
        async with Client(
            create_mcp_server(service=Service(store.root), enable_jobs=True)
        ) as client:
            result = await client.call_tool("get_job", {"job_id": job_id})
            assert not result.is_error
            assert result.structured_content == completed

    anyio.run(exercise)


def test_mcp_controls_match_rest_without_running_workers(retrieval_service):
    api = TestClient(create_app(service=retrieval_service, enable_jobs=True))

    async def exercise():
        async with Client(create_mcp_server(service=retrieval_service, enable_jobs=True)) as client:
            listing = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert CONTROL_TOOLS.issubset(listing)
            assert len(listing) == 16
            for name, tool in listing.items():
                assert tool.input_schema["additionalProperties"] is False
                assert tool.output_schema is not None
                assert tool.annotations.open_world_hint is False
                mutation = name in {"plan_retrieval", "submit_plan", "cancel_job", "retry_job"}
                assert tool.annotations.read_only_hint is not mutation
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.idempotent_hint is (name != "retry_job")
            for tool, arguments, path, verb, body in (
                (
                    "plan_retrieval",
                    {"spec": SPEC, "offline": True},
                    "/retrieval/plans",
                    "post",
                    {"spec": SPEC, "offline": True},
                ),
                ("get_plan", {"plan_id": PLAN_ID}, f"/retrieval/plans/{PLAN_ID}", "get", None),
                (
                    "submit_plan",
                    {"plan_id": PLAN_ID, "idempotency_key": "national-1"},
                    "/retrieval/jobs",
                    "post",
                    {"plan_id": PLAN_ID, "idempotency_key": "national-1"},
                ),
                ("get_job", {"job_id": JOB_ID}, f"/retrieval/jobs/{JOB_ID}", "get", None),
                (
                    "list_jobs",
                    {"limit": 5, "offset": 2},
                    "/retrieval/jobs?limit=5&offset=2",
                    "get",
                    None,
                ),
                (
                    "cancel_job",
                    {"job_id": JOB_ID},
                    f"/retrieval/jobs/{JOB_ID}/cancel",
                    "post",
                    None,
                ),
                ("retry_job", {"job_id": JOB_ID}, f"/retrieval/jobs/{JOB_ID}/retry", "post", None),
            ):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error
                response = getattr(api, verb)(path, **({"json": body} if body else {}))
                assert response.status_code < 300
                assert result.structured_content == response.json()

    anyio.run(exercise)


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("plan_retrieval", {"spec": SPEC, "offline": "true"}),
        ("plan_retrieval", {"spec": SPEC, "offline": True, "refresh": True}),
        ("plan_retrieval", {"spec": SPEC, "worker": "/private/internal/path"}),
        ("plan_retrieval", {"spec": SPEC, "budget": None}),
        ("plan_retrieval", {"spec": SPEC, "budget": {"max_requests": "20"}}),
        ("plan_retrieval", {"spec": SPEC, "budget": {"max_requests": 0}}),
        ("plan_retrieval", {"spec": SPEC, "budget": {"max_attempts": 11}}),
        ("get_plan", {"plan_id": "/private/internal/path"}),
        ("submit_plan", {"plan_id": PLAN_ID, "idempotency_key": ""}),
        ("submit_plan", {"plan_id": PLAN_ID, "idempotency_key": "safe", "execute": True}),
        ("get_job", {"job_id": "/private/internal/path"}),
        ("list_jobs", {"limit": "20"}),
        ("list_jobs", {"limit": 101}),
        ("list_jobs", {"offset": -1}),
        ("cancel_job", {"job_id": "/private/internal/path"}),
        ("retry_job", {"job_id": "/private/internal/path"}),
    ],
)
def test_mcp_control_arguments_are_strict(retrieval_service, tool, arguments):
    async def exercise():
        async with Client(create_mcp_server(service=retrieval_service, enable_jobs=True)) as client:
            result = await client.call_tool(tool, arguments)
            assert result.is_error
            assert "/private/internal/path" not in result.content[0].text

    anyio.run(exercise)
    assert retrieval_service.calls == []


@pytest.mark.parametrize("exception", [KeyError, ValueError, RetrievalError, RuntimeError])
def test_mcp_control_errors_are_sanitized(retrieval_service, exception):
    def fail(*args, **kwargs):
        raise exception("/private/internal/path")

    retrieval_service.get_job = fail

    async def exercise():
        async with Client(create_mcp_server(service=retrieval_service, enable_jobs=True)) as client:
            result = await client.call_tool("get_job", {"job_id": JOB_ID})
            assert result.is_error
            assert "/private/internal/path" not in result.content[0].text

    anyio.run(exercise)


def test_mcp_control_outputs_are_bounded(retrieval_service):
    retrieval_service.get_job = lambda _: {"notes": "x" * 256_001}

    async def exercise():
        async with Client(create_mcp_server(service=retrieval_service, enable_jobs=True)) as client:
            result = await client.call_tool("get_job", {"job_id": JOB_ID})
            assert result.is_error
            assert "Invalid retrieval request" in result.content[0].text

    anyio.run(exercise)


def test_mcp_plan_inputs_are_bounded(retrieval_service):
    large_spec = {
        "name": "large",
        "entries": {
            "observations": {
                "request": {
                    "provider": "nomis",
                    "dataset_id": "NM_1_1",
                    "params": {"notes": "x" * 60_001},
                }
            }
        },
    }

    async def exercise():
        async with Client(create_mcp_server(service=retrieval_service, enable_jobs=True)) as client:
            result = await client.call_tool("plan_retrieval", {"spec": large_spec})
            assert result.is_error
            assert "Invalid retrieval request" in result.content[0].text
            assert len(result.content[0].text) < 1000

    anyio.run(exercise)
    assert retrieval_service.calls == []


def test_mcp_cli_job_control_flag(monkeypatch):
    calls = []

    class Server:
        def run(self, **kwargs):
            calls.append(("run", kwargs))

    def create(store, *, enable_jobs=False):
        calls.append(("create", store, enable_jobs))
        return Server()

    monkeypatch.setattr("ssdu.mcp_server.create_mcp_server", create)
    main(["--store", "local-data", "--enable-jobs"])
    assert calls == [("create", "local-data", True), ("run", {})]


def test_enabled_stdio_controls():
    tests_path = str(Path(__file__).parent)
    source = (
        "import sys; "
        f"sys.path.insert(0, {tests_path!r}); "
        "from test_retrieval_api import FakeRetrievalService; "
        "from ssdu.mcp_server import create_mcp_server; "
        "create_mcp_server(service=FakeRetrievalService(), enable_jobs=True).run()"
    )

    async def exercise():
        parameters = StdioServerParameters(command=sys.executable, args=["-c", source])
        async with Client(parameters, read_timeout_seconds=10) as client:
            assert len((await client.list_tools()).tools) == 16
            result = await client.call_tool("plan_retrieval", {"spec": SPEC, "offline": True})
            assert not result.is_error
            assert result.structured_content["input"]["offline"] is True
            queued = await client.call_tool(
                "submit_plan", {"plan_id": PLAN_ID, "idempotency_key": "stdio-1"}
            )
            assert not queued.is_error
            assert queued.structured_content["status"] == "queued"

    anyio.run(exercise)


def test_actual_service_offline_control_parity_and_persistence(collection_store):
    store, snapshot = collection_store
    service = Service(store.root)
    api = TestClient(create_app(service=service, enable_jobs=True))
    spec = {
        "name": "general-offline-collection",
        "entries": {"observations": {"snapshot_id": snapshot["product_id"]}},
    }
    response = api.post("/retrieval/plans", json={"spec": spec, "offline": True})
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["ready"] and not plan["provider_access_required"]
    assert api.get(f"/retrieval/plans/{plan['plan_id']}").json() == plan
    queued = api.post(
        "/retrieval/jobs",
        json={"plan_id": plan["plan_id"], "idempotency_key": "offline-parity"},
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["status"] == "queued"
    reopened = Service(store.root)
    assert reopened.get_job(job["job_id"]) == job

    async def exercise():
        async with Client(create_mcp_server(service=reopened, enable_jobs=True)) as client:
            planned = await client.call_tool("plan_retrieval", {"spec": spec, "offline": True})
            assert not planned.is_error
            assert planned.structured_content == plan
            submitted = await client.call_tool(
                "submit_plan", {"plan_id": plan["plan_id"], "idempotency_key": "offline-parity"}
            )
            assert not submitted.is_error
            assert submitted.structured_content == job
            for name, args, path in (
                ("get_plan", {"plan_id": plan["plan_id"]}, f"/retrieval/plans/{plan['plan_id']}"),
                ("get_job", {"job_id": job["job_id"]}, f"/retrieval/jobs/{job['job_id']}"),
                ("list_jobs", {}, "/retrieval/jobs"),
            ):
                result = await client.call_tool(name, args)
                assert not result.is_error
                assert result.structured_content == api.get(path).json()
            cancelled = await client.call_tool("cancel_job", {"job_id": job["job_id"]})
            assert not cancelled.is_error
            assert cancelled.structured_content["status"] == "cancelled"
            assert (
                cancelled.structured_content == api.get(f"/retrieval/jobs/{job['job_id']}").json()
            )

    anyio.run(exercise)
