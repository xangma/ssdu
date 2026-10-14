"""Four independent national datasets, durable retrieval and verified offline transfer.

All responses in this tutorial are labelled synthetic teaching fixtures. Values,
measures, dates and geography definitions differ; collection membership does not
make them comparable. Run ``python examples/four_nations.py --output-dir NEW_DIR``.
The destination is verified in another process with network and source-store reads
blocked. The fixture transport uses its own rate limiter and leaves live pacing
settings unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pyarrow as pa
import yaml

from ssdu.bundles import export_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.models import Request
from ssdu.rate_limit import RateLimiter
from ssdu.retrieval import RetrievalService
from ssdu.store import Store, canonical

if __package__:
    from .dataset_contract import FixtureResponses, no_network
else:
    from dataset_contract import FixtureResponses, no_network

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "four-nations"
SPEC = HERE / "collections" / "four-nations.yaml"


def table_hash(table: pa.Table) -> str:
    """Hash logical Arrow cells and types, independently of Parquet encoding."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table.combine_chunks())
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


def fixture_manifest() -> dict:
    """Verify every retained fixture, including reused England/Wales responses."""
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    if manifest["kind"] != "synthetic_teaching_fixtures":
        raise ValueError("Expected labelled synthetic fixture provenance")
    for item in manifest["files"]:
        path = (HERE / item["path"]).resolve()
        if not path.is_relative_to(HERE) or not item["reference"].startswith("https://"):
            raise ValueError("Invalid fixture provenance")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Fixture hash mismatch: {item['path']}")
    return manifest


class FourNationResponses:
    """Closed fixture routing with one deliberate interruption after checkpoints."""

    def __init__(self, *, interrupt_after=2):
        fixture_manifest()
        self.england = FixtureResponses("deprivation")
        self.wales = FixtureResponses("statswales")
        self.calls = []
        self.completed = []
        self.interrupt_after = interrupt_after
        self.interrupted = False

    def __call__(self, request):
        if not self.interrupted and len(self.completed) == self.interrupt_after:
            self.interrupted = True
            raise KeyboardInterrupt("Synthetic worker interruption after retained responses")
        identity = {"method": request.method, "url": str(request.url)}
        self.calls.append(identity)
        if request.url.host == "assets.publishing.service.gov.uk":
            response = self.england(request)
        elif request.url.host == "api.stats.gov.wales":
            response = self.wales(request)
        else:
            response = self.new_provider_response(request)
        response.headers["x-ssdu-fixture"] = "synthetic_teaching_fixtures"
        self.completed.append(identity)
        return response

    def new_provider_response(self, request):
        """Serve the bounded NISRA and Scottish response protocols below."""
        if request.method == "GET" and request.url.host == "ws-data.nisra.gov.uk":
            cube = json.loads((FIXTURES / "nisra-cube.json").read_bytes())
            base = "/public/api.restful/PxStat.Data.Cube_API."
            if request.url.path == base + "ReadMetadata/SYNTH_TS/en":
                return httpx.Response(200, json={**cube, "value": []})
            if request.url.path == base + "ReadDataset/SYNTH_TS/JSON-stat/2.0/en":
                return httpx.Response(200, json=cube)
        if (
            request.method == "GET"
            and request.url.host == "statistics.gov.scot"
            and request.url.path == "/sparql.json"
        ):
            query = request.url.params["query"]
            assert "ssdu-synthetic-series" in query
            name = "metadata" if "AS ?kind" in query else "observations"
            return httpx.Response(200, content=(FIXTURES / f"scotland-{name}.json").read_bytes())
        raise AssertionError(f"Unexpected fixture request: {request.method} {request.url}")


DESTINATION = r"""
import builtins
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

source, fresh, bundle, expected_path = map(Path, sys.argv[1:])
source = source.resolve()
events = []

def deny_network(*args, **kwargs):
    events.append("network")
    raise AssertionError("Offline replay attempted network access")

def guard(event, args):
    if event in {"open", "os.listdir", "os.scandir"} and args:
        value = args[0]
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value)).resolve()
            if path == source or source in path.parents:
                events.append("source_store")
                raise AssertionError("Destination attempted to read the original store")

sys.addaudithook(guard)
socket.getaddrinfo = deny_network
socket.gethostbyname = deny_network
socket.gethostbyname_ex = deny_network
socket.gethostbyaddr = deny_network
socket.create_connection = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network

import intake
import pandas as pd
import pyarrow as pa
from ssdu.bundles import import_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.models import Request
from ssdu.store import Store, canonical

HttpClient.request = deny_network
pa.set_cpu_count(1)
pa.set_io_thread_count(1)
expected = json.loads(expected_path.read_bytes())
store = Store(fresh)
imported = import_bundle(store, bundle)
catalogue = intake.from_yaml_file(imported["catalogue"])

def table_hash(table):
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table.combine_chunks())
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()

providers = {}
for nation, item in expected["nations"].items():
    dataset = store.open_dataset(item["snapshot_id"])
    cached = store.fetch_dataset(Request(**item["request"]), offline=True)
    assert cached.snapshot_id == dataset.snapshot_id
    assert dataset.contract == item["descriptor"]
    hashes = {name: table_hash(dataset.to_arrow(name)) for name in dataset.table_names}
    assert hashes == item["table_hashes"]
    manifest = store.verify(dataset.snapshot_id)
    assert manifest["files"] == item["files"]
    source_frame = pd.read_parquet(store.path(dataset.snapshot_id) / "data.parquet",
                                   use_threads=False, pre_buffer=False)
    loaded = catalogue[f"{manifest['provider']}_{dataset.snapshot_id[:12]}"].read(
        use_threads=False, pre_buffer=False)
    pd.testing.assert_frame_equal(source_frame, loaded)
    providers[nation] = {"raw_equal": True, "normalized_equal": True,
                        "cached_requests": 0, "intake_equal": True}
replayed = Collections(store).replay(expected["lock_id"])
assert hashlib.sha256(canonical(replayed)).hexdigest() == expected["replay_sha256"]
assert events == [], events
print(json.dumps({"verified": True, "pid": os.getpid(), "providers": providers,
                  "network_attempts": 0, "source_store_attempts": 0,
                  "bundle_id": imported["bundle_id"]}), flush=True)
"""


def run(output_dir: str | Path) -> dict:
    """Use real acquisition paths, resume retained work, then transfer offline."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixtures = fixture_manifest()
    spec = yaml.safe_load(SPEC.read_bytes())
    source = Store(output / "source")
    responses = FourNationResponses()
    limiter = RateLimiter(output / "fixture-rate.sqlite3", interval_seconds=0)
    settings = {key: os.environ.get(key) for key in ("SSDU_RATE_DB", "SSDU_RATE_INTERVAL_SECONDS")}

    def client_factory(**kwargs):
        return HttpClient(
            transport=httpx.MockTransport(responses), retries=0, rate_limiter=limiter, **kwargs
        )

    with no_network() as network_attempts:
        worker = RetrievalService(source, client_factory=client_factory)
        plan = worker.plan_retrieval(spec)
        assert plan["ready"], plan
        queued = worker.submit_plan(plan["plan_id"], "four-nation-teaching-example")
        try:
            worker.run_job(queued["job_id"])
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("The deliberate interruption did not occur")
        interrupted = worker.get_job(queued["job_id"])
        assert interrupted["status"] == "queued"
        assert interrupted["entries"]["england"]["state"] == "succeeded"
        retained_responses = list(responses.completed)

        # Construct a new worker against persisted SQLite state and response checkpoints.
        worker = RetrievalService(source, client_factory=client_factory)
        completed = worker.run_job(queued["job_id"])
        assert completed["status"] == "succeeded", completed
        for item in retained_responses:
            # NISRA deliberately reads the same metadata URL before and after its
            # cube; positional checkpoints must reuse the first read only.
            expected_count = 2 if "ReadMetadata/" in item["url"] else 1
            assert responses.completed.count(item) == expected_count
        nations, snapshots = {}, {}

        def forbid_http(*args, **kwargs):
            raise AssertionError("A retained dataset contacted its provider")

        with patch.object(HttpClient, "request", forbid_http):
            for nation, entry in spec["entries"].items():
                request = Request(**entry["request"])
                dataset = source.fetch_dataset(request)
                reopened = source.fetch_dataset(request, offline=True)
                assert reopened.snapshot_id == dataset.snapshot_id
                assert reopened.contract == dataset.contract
                tables = {name: table_hash(dataset.to_arrow(name)) for name in dataset.table_names}
                assert tables == {
                    name: table_hash(reopened.to_arrow(name)) for name in reopened.table_names
                }
                manifest = source.verify(dataset.snapshot_id)
                observations = dataset.to_arrow().to_pylist()
                if nation == "england":
                    values = [row["value"] for row in observations if row["measure"] == "imd_score"]
                    assert values == [4.125, None, 6.25]
                else:
                    expected_values = {
                        "wales": [22.0, None, 1.0],
                        "scotland": [7.0, 0.0],
                        "northern_ireland": [12.0, 13.0],
                    }
                    assert [row["value"] for row in observations] == expected_values[nation]
                snapshots[nation] = {"snapshot_id": dataset.snapshot_id}
                nations[nation] = {
                    "provider": request.provider,
                    "snapshot_id": dataset.snapshot_id,
                    "request": request.to_dict(),
                    "descriptor": dataset.contract,
                    "table_hashes": tables,
                    "files": manifest["files"],
                    "source_rows": dataset.to_arrow("source").num_rows,
                    "observation_rows": dataset.to_arrow().num_rows,
                    "cached_requests": 0,
                    "scope": fixtures["nations"][nation]["scope"],
                }
        lock = Collections(source).resolve({**spec, "entries": snapshots}, offline=True)
        replay = Collections(source).replay(lock.lock_id)
        bundle = output / "four-nations.ssdu.zip"
        exported = export_bundle(source, lock, bundle)
        expected = {
            "nations": nations,
            "lock_id": lock.lock_id,
            "replay_sha256": hashlib.sha256(canonical(replay)).hexdigest(),
        }
        expected_path = output / "expected.json"
        expected_path.write_bytes(canonical(expected) + b"\n")
        assert network_attempts == []
    fresh = output / "fresh"
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            DESTINATION,
            str(source.root),
            str(fresh),
            str(bundle),
            str(expected_path),
        ],
        cwd=output,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    (output / "destination.stdout.json").write_text(process.stdout)
    (output / "destination.stderr.log").write_text(process.stderr)
    if process.returncode:
        raise RuntimeError(f"Fresh-store verification failed:\n{process.stderr}")
    destination = json.loads(process.stdout)
    assert destination["verified"]
    assert destination["bundle_id"] == exported["bundle_id"]
    assert settings == {key: os.environ.get(key) for key in settings}
    result = {
        "verified": True,
        "fixture_kind": fixtures["kind"],
        "comparison_warning": spec["description"],
        "fixtures": fixtures["files"],
        "nations": {
            nation: {
                key: value
                for key, value in details.items()
                if key not in {"files", "descriptor", "request"}
            }
            for nation, details in nations.items()
        },
        "job_id": completed["job_id"],
        "job_status": completed["status"],
        "job_requests": completed["requests"],
        "worker_restarted": True,
        "retained_responses_reused": len(retained_responses),
        "completed_entries_reused": ["england"],
        "collection_lock_id": lock.lock_id,
        "bundle_id": exported["bundle_id"],
        "fresh_store": str(fresh),
        "fresh_store_verified": True,
        "destination": destination,
        "network_attempts": 0,
        "live_rate_settings_unchanged": True,
    }
    (output / "result.json").write_bytes(canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="A directory that does not exist"
    )
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
