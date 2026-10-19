"""Capture and move a deep, entirely invented metadata catalogue offline.

This example never contacts a provider or retrieves observation records. It
teaches durable metadata-only jobs: enumerate a catalogue, retain its tables,
variables, explicit questions and response codes, then transfer the verified
snapshot to a fresh process. All HTTP responses are invented in-memory fixtures.

Run ``python metadata_graph.py --output-dir NEW_DIRECTORY`` after installing
SSDU. The directory must be absent or empty. A compact ``result.json`` records
the checks; the retained source responses and bundles remain in that directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from ssdu.http import HttpClient
from ssdu.metadata_graph import GRAPH_CONTRACT_VERSION
from ssdu.metadata_graph_capture import MetadataGraphCapture
from ssdu.metadata_graph_store import MetadataGraphStore
from ssdu.rate_limit import RateLimiter

ORIGINS = ("02", "03")
SPEC = {
    "connector": "adr_uk",
    "page_size": 1,
    "max_pages": 50,
    "max_tasks": 100,
    "max_entities": 1000,
}


def table():
    return {
        "elementType": "DataClass",
        "id": "010",
        "name": "Invented community questionnaire table",
        "childClasses": False,
        "dataElements": [
            {
                "elementType": "DataElement",
                "id": "020",
                "name": "COMMUNITY",
                "dataType": {"name": "InventedCommunityType"},
            }
        ],
    }


def envelope(rows, request, *, inventory=False):
    """Honor the two different native page-number conventions explicitly."""
    number = int(request.url.params["pageNumber" if inventory else "page"])
    size = int(request.url.params["pageSize" if inventory else "size"])
    offset = (number - 1 if inventory else number) * size
    content = rows[offset : offset + size]
    if inventory:
        return {
            "content": content,
            "totalCount": len(rows),
            "totalPages": (len(rows) + size - 1) // size,
            "pageSize": size,
            "pageCount": len(content),
            "currentPage": number,
        }
    return {
        "resourceIdentifier": {"id": "001", "originId": request.url.params["originId"]},
        "content": content,
        "pageable": {"number": number, "size": size},
        "totalSize": len(rows),
        "totalPages": (len(rows) + size - 1) // size,
        "numberOfElements": len(content),
        "pageNumber": number,
        "empty": not content,
        "size": size,
        "offset": offset,
    }


def fixture_response(request):
    """Serve only invented metadata; any unrecognized request fails closed."""
    assert request.method == "GET", "This example must never request observation records"
    assert request.url.host == "api-datacatalogue.adruk.org"
    path = request.url.path
    if path.endswith("/dataset"):
        rows = [
            {
                "id": "001",
                "title": f"Invented community study from service {origin}",
                "abstract": "Independently invented metadata, with no participant records.",
                "origin": {"id": origin, "name": f"Invented service {origin}"},
            }
            for origin in ORIGINS
        ]
        return httpx.Response(200, json=envelope(rows, request, inventory=True))
    origin = request.url.params.get("originId", request.url.params.get("origin"))
    assert origin in ORIGINS, f"Unexpected invented origin: {origin}"
    if path.endswith("/dataset/001"):
        payload = {
            "id": "001",
            "origin": {"id": origin, "name": f"Invented service {origin}"},
            "summary": {
                "title": f"Invented community study from service {origin}",
                "abstract": "A metadata-only teaching catalogue; no data observations exist.",
            },
            "documentation": {
                "description": "Invented descriptions, question text and missing-value meanings."
            },
            "structuralMetadata": [table()],
            "versionHistory": [
                {"semanticVersion": version, "dataModelId": "001", "public": True}
                for version in ("1.0.0", "2.0.0")
            ],
            "accessAndGovernance": {
                "dataAccessAddress": "https://example.invalid/invented-application",
                "dataAccessConditions": "Invented restricted data; metadata browsing only.",
            },
        }
    elif path.endswith("/structuralMetadata/dataDictionary/001"):
        payload = envelope(
            [
                {
                    "id": "020",
                    "name": "COMMUNITY",
                    "description": "Invented coded community response.",
                    "dataType": "dataElement",
                    "parentId": "010",
                }
            ],
            request,
        )
    elif path.endswith("/structuralMetadata/001"):
        payload = envelope([table()], request)
    elif path.endswith("/dataElement/020"):
        payload = {
            "elementType": "DataElement",
            "id": "020",
            "name": "COMMUNITY",
            "description": "Invented coded community response; codes are text, not counts.",
            "dataType": {
                "name": "InventedCommunityType",
                "basedOn": "Varchar",
                "enumerations": [
                    {"name": "01", "description": "Invented urban community", "isMissing": False},
                    {"name": "99", "description": "Not stated", "isMissing": True},
                ],
            },
            "question": {
                "id": "Q01",
                "text": "Which invented community do you belong to?",
            },
        }
    else:
        raise AssertionError(f"Unexpected metadata request: {request.url}")
    return httpx.Response(200, json=payload)


@contextmanager
def no_network(events, *, block_http=False):
    """Fail closed on accidental network access, including DNS and raw sockets."""

    def denied(*args, **kwargs):
        events.append("network")
        raise AssertionError("This synthetic example permits no network access")

    with ExitStack() as stack:
        for name in ("getaddrinfo", "create_connection"):
            stack.enter_context(patch.object(socket, name, denied))
        for name in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, denied))
        if block_http:
            stack.enter_context(patch.object(HttpClient, "request", denied))
            stack.enter_context(patch.object(httpx.Client, "send", denied))
            stack.enter_context(patch.object(httpx.AsyncClient, "send", denied))
        yield


def all_rows(store, snapshot_id, method):
    """Read a paginated immutable graph without dropping later pages."""
    rows, offset = [], 0
    while True:
        page = getattr(store, method)(snapshot_id, limit=100, offset=offset)
        rows.extend(page["items"])
        offset = page["next_offset"]
        if offset is None:
            return rows


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_graph(store, snapshot_id):
    store.verify(snapshot_id)
    return {
        "entities": all_rows(store, snapshot_id, "entities"),
        "links": all_rows(store, snapshot_id, "links"),
    }


def completed(job):
    assert job["status"] in {"succeeded", "partial"}, job
    report = job["result"]["report"]
    assert not report["failures"], report
    assert not report["unresolved_references"], report
    return job["result"]["snapshot_id"]


def graph_checks(graph):
    entities = graph["entities"]
    counts = {
        kind: sum(row["kind"] == kind for row in entities)
        for kind in sorted({row["kind"] for row in entities})
    }
    assert {"dataset", "table", "variable", "question", "code_list", "code"} <= counts.keys()
    datasets = [row for row in entities if row["kind"] == "dataset"]
    assert len(datasets) == 2
    assert {row["origin"] for row in datasets} == set(ORIGINS)
    assert {row["native_id"] for row in datasets} == {"001"}
    assert len({row["id"] for row in datasets}) == 2, "Origin is part of entity identity"
    questions = [row for row in entities if row["kind"] == "question"]
    assert {row["label"] for row in questions} == {"Which invented community do you belong to?"}
    codes = [row for row in entities if row["kind"] == "code"]
    assert {row["attributes"]["value"] for row in codes} == {"01", "99"}
    assert all(
        row["attributes"]["isMissing"] is (row["attributes"]["value"] == "99") for row in codes
    ), "Missing-value meaning must remain distinct from a substantive response"
    assert all(row["label"] == "Not stated" for row in codes if row["attributes"]["value"] == "99")
    assert all(
        ref["captured_at"] and ref["checked_at"] for row in entities for ref in row["provenance"]
    ), "Capture and check clocks must be retained, not regenerated on replay"
    return counts


def replay_offline(output):
    """Import and replay in a separate process with every HTTP path blocked."""
    output = Path(output).resolve()
    assert not (output / "source").exists(), "The original store must be unavailable"
    expected = json.loads((output / "expected-graph.json").read_text(encoding="utf-8"))
    events = []
    with no_network(events, block_http=True):
        store = MetadataGraphStore(output / "fresh")
        imported_id = store.import_bundle(output / "invented-metadata-graph.ssdu-metadata.zip")
        imported = read_graph(store, imported_id)
        assert imported == expected
        capture = MetadataGraphCapture(output / "fresh")
        plan = capture.plan_capture(SPEC, offline=True)
        job = capture.submit_plan(plan["plan_id"], "invented-offline-replay")
        replay_id = completed(capture.run_job(job["job_id"]))
        replay = read_graph(store, replay_id)
        assert replay == expected
        graph_checks(replay)
    assert events == []
    return {
        "offline_snapshot_id": replay_id,
        "fresh_store_verified": True,
        "offline_replay_equal": True,
        "source_timestamps_preserved": True,
        "offline_worker_pid": os.getpid(),
        "network_attempts": len(events),
    }


def run(output_dir):
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Choose an absent or empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    calls, events = [], []

    def transport(request):
        calls.append(str(request.url))
        return fixture_response(request)

    def client_factory(**kwargs):
        # Zero spacing is restricted to invented responses in this private example.
        kwargs["rate_limiter"] = RateLimiter(output / "fixture-rate.sqlite3", interval_seconds=0)
        return HttpClient(
            transport=httpx.MockTransport(transport),
            **kwargs,
        )

    source = output / "source"
    with no_network(events):
        capture = MetadataGraphCapture(source, client_factory=client_factory)
        plan = capture.plan_capture(SPEC)
        assert calls == [], "Planning must be entirely local"
        job = capture.submit_plan(plan["plan_id"], "invented-resumable-capture")
        paused = capture.run_job(job["job_id"], max_tasks=1)
        assert paused["status"] == "queued", paused
        assert paused["tasks"].get("done") == 1, paused
        retained_calls = list(calls)
        assert retained_calls
        capture = MetadataGraphCapture(source, client_factory=client_factory)
        snapshot_id = completed(capture.run_job(job["job_id"]))
        assert not set(retained_calls).intersection(calls[len(retained_calls) :])
        store = MetadataGraphStore(source)
        graph = read_graph(store, snapshot_id)
        counts = graph_checks(graph)
        report = store.report(snapshot_id)
        (output / "capture-report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

        # A distinct campaign reuses exact retained requests without issuing HTTP.
        before = len(calls)
        cached_job = capture.submit_plan(plan["plan_id"], "invented-cached-capture")
        cached_id = completed(capture.run_job(cached_job["job_id"]))
        assert len(calls) == before
        assert read_graph(store, cached_id) == graph
        bundle = output / "invented-metadata-graph.ssdu-metadata.zip"
        store.export(snapshot_id, bundle)
        (output / "expected-graph.json").write_text(
            json.dumps(graph, indent=2) + "\n", encoding="utf-8"
        )
        source.rename(output / "retained-original-store")
        worker = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_offline-worker", str(output)],
            cwd=output,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert worker.returncode == 0, worker.stdout + worker.stderr
        replay = json.loads(worker.stdout)
        assert replay["offline_worker_pid"] != os.getpid()
    assert events == []
    receipt = {
        "status": "passed",
        "fixture_kind": "independently_invented_teaching_metadata",
        "graph_contract_version": GRAPH_CONTRACT_VERSION,
        "connector": SPEC["connector"],
        "snapshot_id": snapshot_id,
        "capture_status": report["status"],
        "metadata_scope": report["scope"],
        "all_provider_holdings_percent": None,
        "entity_counts": counts,
        "graph_sha256": digest(graph),
        "interruption_resumed": True,
        "retained_prefix_not_refetched": True,
        "cached_capture_zero_http": True,
        "metadata_only": True,
        "fixture_http_requests": len(calls),
        "observation_requests": 0,
        "original_store_path_unavailable": not source.exists(),
        **replay,
    }
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--_offline-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._offline_worker is not None:
        result = replay_offline(args._offline_worker)
    elif args.output_dir is not None:
        result = run(args.output_dir)
    else:
        parser.error("--output-dir is required")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
