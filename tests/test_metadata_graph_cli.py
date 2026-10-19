"""The public CLI plans locally and transfers deep metadata without acquisition."""

import hashlib
import json
import socket

import pytest

from ssdu.cli import main
from ssdu.http import HttpClient, Resource
from ssdu.metadata_graph import make_entity
from ssdu.metadata_graph_store import MetadataGraphStore


@pytest.fixture
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("A local metadata graph operation attempted network access")

    monkeypatch.setattr(HttpClient, "request", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def invoke(capsys, root, *command):
    assert main(["--store", str(root), "metadata", "graph", *map(str, command)]) == 0
    return json.loads(capsys.readouterr().out)


def test_graph_cli_plans_and_submits_without_http(tmp_path, capsys, offline):
    connectors = invoke(capsys, tmp_path, "connectors")
    assert {item["id"] for item in connectors["items"]} >= {
        "adr_uk",
        "nisra_secure",
        "research_data_scotland",
    }
    schema = invoke(capsys, tmp_path, "schema")
    assert schema
    spec = tmp_path / "capture.json"
    spec.write_text(json.dumps({"connector": "adr_uk", "page_size": 10}))
    plan = invoke(capsys, tmp_path, "plan", spec)
    assert plan["ready"] is True
    assert invoke(capsys, tmp_path, "plan-show", plan["plan_id"]) == plan
    job = invoke(capsys, tmp_path, "submit", plan["plan_id"], "--idempotency-key", "cli-lesson")
    assert job["status"] == "queued" and job["requests"] == 0
    again = invoke(capsys, tmp_path, "submit", plan["plan_id"], "--idempotency-key", "cli-lesson")
    assert again["job_id"] == job["job_id"]
    assert invoke(capsys, tmp_path, "job", job["job_id"])["status"] == "queued"


def test_graph_cli_bundle_reopens_without_original_store(tmp_path, capsys, offline):
    root = tmp_path / "original"
    raw = b'{"question":"Which fictional service was used?"}'
    url = "https://api-datacatalogue.adruk.org/api/dataElement/0001?origin=teaching"
    resource = Resource(
        raw,
        url,
        "GET",
        None,
        "application/json",
        {},
        captured_at="2026-01-01T00:00:00+00:00",
        checked_at="2026-01-01T00:00:00+00:00",
    )
    entity = make_entity(
        "adr_uk",
        "teaching",
        "question",
        "0001/question",
        label="Invented service question",
        provenance=[
            {
                "url": url,
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "pointer": "/question",
                "captured_at": resource.captured_at,
                "checked_at": resource.checked_at,
            }
        ],
    )
    snapshot_id = MetadataGraphStore(root).publish([entity], [], [resource], {"status": "complete"})
    bundle = tmp_path / "teaching.zip"
    invoke(capsys, root, "export", snapshot_id, "--output", bundle)
    root.rename(tmp_path / "original-removed")
    fresh = tmp_path / "fresh"
    assert invoke(capsys, fresh, "import", bundle)["snapshot_id"] == snapshot_id
    page = invoke(capsys, fresh, "entities", snapshot_id, "--kind", "question")
    assert page["items"] == [entity]
    assert invoke(capsys, fresh, "entity", snapshot_id, entity["id"]) == entity
    assert invoke(capsys, fresh, "links", snapshot_id)["items"] == []
    assert invoke(capsys, fresh, "report", snapshot_id)["status"] == "complete"
