"""Native new-provider requests share the real Python, CLI, REST and MCP planner."""

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from ssdu.api import create_app
from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.mcp_server import create_mcp_server
from ssdu.service import Service


def test_new_provider_plans_and_queued_jobs_match_all_interfaces(tmp_path, monkeypatch, capsys):
    def deny(*args, **kwargs):
        pytest.fail("Planning or queueing unexpectedly contacted a provider")

    monkeypatch.setattr(HttpClient, "request", deny)
    root = tmp_path / "store"
    service = Service(root, index_store=False)
    spec = {
        "name": "synthetic-national-series",
        "entries": {
            "ni": {"request": {"provider": "nisra", "dataset_id": "SYNTH_TS"}},
            "scotland": {
                "request": {"provider": "scotland", "dataset_id": "ssdu-synthetic-series"}
            },
        },
    }
    expected = service.plan_retrieval(spec)
    assert expected["ready"], expected
    assert {entry["action"] for entry in expected["entries"].values()} == {"fetch"}
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(spec))
    assert main(["--store", str(root), "retrieval", "plan", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == expected

    api = TestClient(create_app(service=service, enable_jobs=True))
    response = api.post("/retrieval/plans", json={"spec": spec})
    assert response.status_code == 201, response.text
    assert response.json() == expected
    arguments = {"plan_id": expected["plan_id"], "idempotency_key": "new-provider-plan"}
    response = api.post("/retrieval/jobs", json=arguments)
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["status"] == "queued" and queued["requests"] == 0

    async def exercise():
        async with Client(create_mcp_server(service=service, enable_jobs=True)) as client:
            plan = await client.call_tool("plan_retrieval", {"spec": spec})
            assert not plan.is_error and plan.structured_content == expected
            job = await client.call_tool("submit_plan", arguments)
            assert not job.is_error and job.structured_content == queued

    anyio.run(exercise)
    assert Service(root, index_store=False).get_job(queued["job_id"]) == queued
