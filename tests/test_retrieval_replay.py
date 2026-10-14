"""Exercise durable CLI process boundaries and portable local-only collection replay."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from ssdu.local_import import import_local
from ssdu.models import Acquisition, Request
from ssdu.store import Store

EXAMPLE = Path(__file__).parents[1] / "examples" / "retrieval_jobs.py"


@pytest.mark.parametrize("request_entry", [False, True], ids=["local-only", "cached-request"])
def test_collection_job_survives_processes_and_replans_in_fresh_store(
    tmp_path, monkeypatch, request_entry
):
    source = Store(tmp_path / "source")
    csv = tmp_path / "study-notes.csv"
    csv.write_text("study_id,score\n001,7\n002,9\n")
    notes = import_local(
        source,
        csv,
        {
            "dataset_id": "study-notes",
            "title": "Synthetic study notes without geography",
            "licence": "CC0-1.0",
            "citation": "Synthetic offline acceptance fixture",
            "column_types": {"study_id": "string", "score": "int64"},
        },
    )
    if request_entry:

        class Adapter:
            @staticmethod
            def fetch(request, client):
                return Acquisition(
                    request.provider,
                    request.dataset_id,
                    "Synthetic cached table without geography",
                    pd.DataFrame({"study_id": ["001", "002"], "score": [7, 9]}),
                    {},
                    {"edition": "synthetic"},
                    "CC0-1.0",
                    "Synthetic offline acceptance fixture",
                    "https://statswales.gov.wales/fixture",
                )

        monkeypatch.setattr("ssdu.store.adapter", lambda _: Adapter)
        notes = source.fetch(Request("statswales", "00000000-0000-0000-0000-000000000001"))
    parquet = tmp_path / "methods.parquet"
    pd.DataFrame(
        {"method": ["survey", "interview", "observation"], "count": [12, 8, 3]}
    ).to_parquet(parquet, index=False)
    methods = import_local(
        source,
        parquet,
        {
            "dataset_id": "study-methods",
            "title": "Synthetic research methods without geography",
            "licence": "CC0-1.0",
            "citation": "Synthetic offline acceptance fixture",
        },
    )
    spec = tmp_path / "collection.json"
    spec.write_text(
        json.dumps(
            {
                "name": "research-inputs",
                "description": "Independent local tables; no geographic requirement",
                "entries": {
                    "notes": (
                        {"request": notes["request"]}
                        if request_entry
                        else {"snapshot_id": notes["product_id"]}
                    ),
                    "methods": {"snapshot_id": methods["product_id"]},
                },
            }
        )
    )
    before = {path: path.read_bytes() for path in (csv, parquet, spec)}
    process = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE),
            "--store",
            str(source.root),
            "--spec",
            str(spec),
            "--output",
            str(tmp_path / "evidence"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["status"] == "succeeded"
    assert report["provider_requests"] == report["provider_bytes"] == 0
    assert report["fresh_job_status"] == "succeeded"
    assert report["fresh_provider_requests"] == report["fresh_provider_bytes"] == 0
    assert report["fresh_job_id"] != report["job_id"]
    assert report["fresh_job_reuses_imported_lock"]
    assert report["collection"] == "research-inputs"
    assert report["entry_rows"] == {
        "notes": {"total": 2, "returned": 2},
        "methods": {"total": 3, "returned": 3},
    }
    assert report["snapshot_count_including_parents"] == 2
    assert report["intake_snapshot_count"] == 2
    assert report["replay_equal"] and report["intake_roundtrip"] == "passed"
    assert report["queued_job_survived_process_restart"]
    assert report["completed_job_survived_process_restart"]
    assert report["idempotent_submission"]
    assert report["existing_snapshot_hashes_unchanged"]
    assert report["guard_attempts"] == 0
    phases = report["separate_processes"]
    assert len({phase["pid"] for phase in phases}) == len(phases)
    assert all(phase["exit_code"] == 0 for phase in phases)
    assert {phase["phase"] for phase in phases} >= {
        "plan",
        "submit",
        "show-queued",
        "submit-again",
        "work",
        "show-completed",
        "destination-replay",
        "destination-plan",
        "destination-submit",
        "destination-work",
        "destination-job-replay",
        "intake",
    }
    destination = Store(report["fresh_store"])
    assert len(destination.list()) == 2
    for manifest in (notes, methods):
        assert destination.verify(manifest["product_id"])["files"] == manifest["files"]
    assert all(path.read_bytes() == body for path, body in before.items())


def test_offline_acceptance_guards_reject_network_and_source_store(tmp_path):
    spec = importlib.util.spec_from_file_location("retrieval_jobs_acceptance", EXAMPLE)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    guards = tmp_path / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(example.GUARD)
    attempts = tmp_path / "attempts.jsonl"
    forbidden = tmp_path / "source"
    env = {
        **os.environ,
        "PYTHONPATH": str(guards) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "SSDU_RETRIEVAL_GUARD_LOG": str(attempts),
        "SSDU_RETRIEVAL_FORBIDDEN_STORE": str(forbidden),
    }
    env.pop("SSDU_RETRIEVAL_GUARD_ACTIVE", None)
    code = """
import os
import socket
from ssdu.http import HttpClient
from ssdu.store import Store
assert os.environ.get('SSDU_RETRIEVAL_GUARD_ACTIVE') == '1'
checks = [
    lambda: HttpClient.request(None),
    lambda: socket.getaddrinfo('example.invalid', 443),
    lambda: socket.gethostbyname('example.invalid'),
    lambda: socket.gethostbyname_ex('example.invalid'),
    lambda: socket.gethostbyaddr('127.0.0.1'),
    lambda: socket.create_connection(('example.invalid', 443)),
    lambda: Store(os.environ['SSDU_RETRIEVAL_FORBIDDEN_STORE']),
]
with socket.socket() as connection:
    checks += [lambda: connection.connect(('127.0.0.1', 9)),
               lambda: connection.connect_ex(('127.0.0.1', 9))]
    for check in checks:
        try:
            check()
        except AssertionError:
            continue
        raise AssertionError('A guard allowed a prohibited operation')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in attempts.read_text().splitlines()]
    assert len(events) == 9
    assert sum(event["kind"] == "network" for event in events) == 8
    assert sum(event["kind"] == "source_store" for event in events) == 1
    assert not forbidden.exists()
