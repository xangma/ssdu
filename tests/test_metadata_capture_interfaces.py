"""Synthetic catalogue capture interfaces never acquire metadata implicitly."""

import json
import socket

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from test_metadata_sync import sync_for

from ssdu.api import create_app
from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.mcp_server import create_mcp_server
from ssdu.metadata_capture import MetadataCapture
from ssdu.service import Service


@pytest.fixture
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Capture interface attempted implicit provider access")

    monkeypatch.setattr(HttpClient, "request", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)


def request():
    return {"spec": {"providers": {"nomis": {}}}, "offline": True, "batch_size": 1}


def test_capture_plan_controls_are_local_strict_and_not_a_worker(tmp_path, no_network):
    service = Service(tmp_path, index_store=False)
    readonly = TestClient(create_app(service=service))
    assert readonly.post("/metadata/capture/plans", json=request()).status_code == 404
    api = TestClient(create_app(service=service, enable_jobs=True))
    response = api.post("/metadata/capture/plans", json=request())
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan == service.plan_metadata_capture(request()["spec"], offline=True, batch_size=1)
    assert api.get(f"/metadata/capture/plans/{plan['plan_id']}").json() == plan
    submitted = api.post(
        "/metadata/capture/jobs",
        json={"plan_id": plan["plan_id"], "idempotency_key": "invented-capture"},
    )
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert api.get(f"/metadata/capture/jobs/{job['job_id']}").json() == job
    assert api.get("/metadata/capture/jobs").json()["items"][0]["job_id"] == job["job_id"]
    assert api.post(f"/metadata/capture/jobs/{job['job_id']}/run").status_code == 404
    assert readonly.get(f"/metadata/capture/jobs/{job['job_id']}").status_code == 404
    assert (
        api.post(f"/metadata/capture/jobs/{job['job_id']}/cancel").json()["status"] == "cancelled"
    )

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "metadata_capture_report" in names
            assert "plan_metadata_capture" not in names
            assert "run_metadata_capture_job" not in names
            assert "export_metadata_capture" not in names
            assert "import_metadata_capture" not in names
        async with Client(create_mcp_server(service=service, enable_jobs=True)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            result = await client.call_tool("plan_metadata_capture", request())
            assert not result.is_error and result.structured_content == plan
            result = await client.call_tool("get_metadata_capture_job", {"job_id": job["job_id"]})
            assert (
                not result.is_error
                and result.structured_content == service.get_metadata_capture_job(job["job_id"])
            )
            for name in (
                "plan_metadata_capture",
                "submit_metadata_capture_plan",
                "cancel_metadata_capture_job",
                "retry_metadata_capture_job",
            ):
                assert not tools[name].annotations.read_only_hint
                assert not tools[name].annotations.open_world_hint
                assert tools[name].input_schema["additionalProperties"] is False
            assert tools["metadata_capture_report"].annotations.read_only_hint
            assert not tools["metadata_capture_report"].annotations.open_world_hint
            assert not tools["retry_metadata_capture_job"].annotations.idempotent_hint
            assert "run_metadata_capture_job" not in tools
            invalid = [
                {**request(), "refresh": True},
                {**request(), "batch_size": True},
                {**request(), "batch_size": "1"},
                {**request(), "batch_size": 101},
                {**request(), "execute": True},
                {"spec": {"providers": {"unknown": {}}}},
                {"spec": {"providers": {"nomis": {"dataset_ids": ["invented"]}}}},
            ]
            for body in invalid:
                assert (await client.call_tool("plan_metadata_capture", body)).is_error

    anyio.run(exercise)
    for body in (
        {**request(), "refresh": True},
        {**request(), "batch_size": 0},
        {**request(), "execute": True},
    ):
        assert api.post("/metadata/capture/plans", json=body).status_code == 422
    assert api.get("/metadata/capture/jobs", params={"limit": 101}).status_code == 422
    assert api.get("/metadata/capture/jobs", params={"refresh": True}).status_code == 422
    assert api.get("/metadata/captures/not-a-hash").status_code == 422
    assert api.post("/metadata/capture/plans", content=b"x" * 64_001).status_code == 413


def test_capture_mcp_and_rest_share_default_budget(tmp_path, no_network):
    service = Service(tmp_path, index_store=False)
    body = {"spec": {"providers": {"nomis": {}}}, "offline": True}
    api = TestClient(create_app(service=service, enable_jobs=True))
    plan = api.post("/metadata/capture/plans", json=body).json()
    assert plan["input"]["budget"]["max_requests"] == 10_000

    async def exercise():
        async with Client(create_mcp_server(service=service, enable_jobs=True)) as client:
            result = await client.call_tool("plan_metadata_capture", body)
            assert not result.is_error and result.structured_content == plan

    anyio.run(exercise)


def test_capture_cli_transfer_report_and_index_survive_original_removal(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "original"
    sync, calls, clock = sync_for(root)
    capture = MetadataCapture(root, clock=clock, client_factory=sync.client_factory)
    plan = capture.plan_capture({"providers": {"nomis": {}}}, batch_size=1)
    job = capture.submit_plan(plan["plan_id"], "synthetic-interface")
    job = capture.run_job(job["job_id"])
    assert job["status"] == "succeeded", job
    capture_id = job["result"]["capture_id"]
    assert calls

    def fail(*args, **kwargs):
        pytest.fail("Capture transfer, report or search attempted provider access")

    monkeypatch.setattr(HttpClient, "request", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)

    def command(store, *args):
        assert main(["--store", str(store), "metadata", "capture", *args]) == 0
        return json.loads(capsys.readouterr().out)

    bundle = tmp_path / "invented-capture.zip"
    original = command(root, "report", capture_id)
    command(root, "export", capture_id, "--output", str(bundle))
    root.rename(tmp_path / "original-unavailable")
    fresh = tmp_path / "fresh"
    imported = command(fresh, "import", str(bundle))
    assert imported["capture_id"] == capture_id
    assert command(fresh, "report", capture_id) == original
    service = Service(fresh, index_store=False)
    assert service.search_metadata("workshop")["total"] == 1
    assert (
        TestClient(create_app(service=service)).get(f"/metadata/captures/{capture_id}").json()
        == original
    )

    async def exercise():
        async with Client(create_mcp_server(service=service)) as client:
            result = await client.call_tool("metadata_capture_report", {"capture_id": capture_id})
            assert not result.is_error and result.structured_content == original

    anyio.run(exercise)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"providers": {"nomis": {}}}))
    plan = command(fresh, "plan", str(spec), "--offline", "--batch-size", "1")
    assert command(fresh, "plan-show", plan["plan_id"]) == plan
    queued = command(fresh, "submit", plan["plan_id"], "--idempotency-key", "offline-cli")
    assert command(fresh, "job", queued["job_id"])["job_id"] == queued["job_id"]
    assert command(fresh, "jobs")["total"] == 1
    replayed = command(fresh, "run", queued["job_id"])
    assert replayed["status"] == "succeeded", replayed
    assert replayed["requests"] == replayed["bytes"] == 0
    spec.write_bytes(b" " * 60_001)
    assert main(["--store", str(fresh), "metadata", "capture", "plan", str(spec)]) != 0
    assert "60 KB" in capsys.readouterr().err


def test_legacy_service_does_not_advertise_capture_methods():
    class Legacy:
        pass

    paths = create_app(service=Legacy(), enable_jobs=True).openapi()["paths"]
    assert not any("capture" in path for path in paths)
