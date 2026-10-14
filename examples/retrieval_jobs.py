"""Plan and run any cached collection, then transfer and replay it offline.

Run: uv run --offline python examples/retrieval_jobs.py
Use --spec PATH and --store PATH for another collection. Every CLI operation runs
in a fresh Python process with HTTP, DNS and socket connection guards installed.
The default collection requires previously retained provider snapshots.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ssdu.store import Store, checksum

GUARD = """
import json
import os
import socket
from pathlib import Path
from ssdu.http import HttpClient
from ssdu.store import Store

def denied(*args, **kwargs):
    with open(os.environ['SSDU_RETRIEVAL_GUARD_LOG'], 'a') as stream:
        stream.write(json.dumps({'pid': os.getpid(), 'kind': 'network'}) + '\\n')
    raise AssertionError('Offline retrieval workflow attempted network access')

HttpClient.request = denied
socket.getaddrinfo = denied
socket.gethostbyname = denied
socket.gethostbyname_ex = denied
socket.gethostbyaddr = denied
socket.create_connection = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
original_init = Store.__init__

def init(self, root='data'):
    forbidden = os.environ.get('SSDU_RETRIEVAL_FORBIDDEN_STORE')
    if forbidden and Path(root).resolve() == Path(forbidden).resolve():
        with open(os.environ['SSDU_RETRIEVAL_GUARD_LOG'], 'a') as stream:
            stream.write(json.dumps({'pid': os.getpid(), 'kind': 'source_store'}) + '\\n')
        raise AssertionError('Fresh-store replay opened the original store')
    original_init(self, root)

Store.__init__ = init
os.environ['SSDU_RETRIEVAL_GUARD_ACTIVE'] = '1'
"""

CLI = """
import os
import runpy
assert os.environ.get('SSDU_RETRIEVAL_GUARD_ACTIVE') == '1', 'Guard did not initialize'
runpy.run_module('ssdu.cli', run_name='__main__')
"""

INTAKE = """
import json
import os
import sys
assert os.environ.get('SSDU_RETRIEVAL_GUARD_ACTIVE') == '1', 'Guard did not initialize'
import intake
import pandas as pd
import pyarrow as pa
from ssdu.store import Store
# Keep verification processes small and avoid Arrow background-I/O teardown races.
pa.set_cpu_count(1)
pa.set_io_thread_count(1)
store = Store(sys.argv[1])
catalogue = intake.from_yaml_file(sys.argv[2])
snapshot_ids = json.loads(sys.argv[3])
rows = {}
for snapshot_id in snapshot_ids:
    manifest = store.verify(snapshot_id)
    loaded = catalogue[f"{manifest['provider']}_{snapshot_id[:12]}"].read(
        use_threads=False, pre_buffer=False)
    frame = pd.read_parquet(store.path(snapshot_id) / 'data.parquet',
                            use_threads=False, pre_buffer=False)
    pd.testing.assert_frame_equal(loaded, frame)
    rows[snapshot_id] = len(frame)
print(json.dumps({'verified': True, 'rows': rows}), flush=True)
"""


def run(store_path, spec_path, output=None):
    """Return acceptance evidence; require retained inputs and never fetch online."""
    store = Store(store_path)
    spec_path = Path(spec_path).resolve()
    before = {item["product_id"]: item["files"] for item in store.list()}
    base = Path(output) if output else store.root / "validation" / "retrieval-jobs"
    base.mkdir(parents=True, exist_ok=True)
    run_path = Path(tempfile.mkdtemp(prefix="run-", dir=base)).resolve()
    fresh = run_path / "fresh-store"
    fresh.mkdir()
    assert not list(fresh.iterdir())
    guards = run_path / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(GUARD)
    guard_log = run_path / "guard-attempts.jsonl"
    guard_log.write_text("")
    env = {
        **os.environ,
        "PYTHONPATH": str(guards) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "SSDU_RETRIEVAL_GUARD_LOG": str(guard_log),
    }
    # Require each child to establish its own guard rather than inheriting a marker.
    env.pop("SSDU_RETRIEVAL_GUARD_ACTIVE", None)
    env.pop("SSDU_RETRIEVAL_FORBIDDEN_STORE", None)
    processes = []

    def child(phase, code, arguments, *, forbid_source=False):
        child_env = dict(env)
        if forbid_source:
            child_env["SSDU_RETRIEVAL_FORBIDDEN_STORE"] = str(store.root.resolve())
        process = subprocess.Popen(
            [sys.executable, "-c", code, *map(str, arguments)],
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            (run_path / f"{phase}.stdout.json").write_text(stdout)
            (run_path / f"{phase}.stderr.log").write_text(stderr)
            raise RuntimeError(f"Offline acceptance timed out during {phase}") from None
        processes.append({"phase": phase, "pid": process.pid, "exit_code": process.returncode})
        (run_path / f"{phase}.stdout.json").write_text(stdout)
        (run_path / f"{phase}.stderr.log").write_text(stderr)
        if process.returncode:
            raise RuntimeError(f"Offline acceptance failed during {phase}:\n{stderr}")
        return json.loads(stdout)

    def cli(phase, root, arguments, *, forbid_source=False):
        return child(phase, CLI, ["--store", root, *arguments], forbid_source=forbid_source)

    plan = cli("plan", store.root, ["retrieval", "plan", spec_path, "--offline"])
    plan_id = plan["plan_id"]
    key = f"offline-acceptance-{run_path.name}"
    queued = cli("submit", store.root, ["retrieval", "submit", plan_id, "--idempotency-key", key])
    assert queued["status"] == "queued"
    job_id = queued["job_id"]
    persisted = cli("show-queued", store.root, ["jobs", "show", job_id])
    assert persisted["job_id"] == job_id and persisted["status"] == "queued"
    duplicate = cli(
        "submit-again", store.root, ["retrieval", "submit", plan_id, "--idempotency-key", key]
    )
    assert duplicate["job_id"] == job_id
    worked = cli("work", store.root, ["jobs", "work", job_id])
    assert worked["status"] == "succeeded"
    completed = cli("show-completed", store.root, ["jobs", "show", job_id])
    assert completed["status"] == "succeeded"
    assert completed["requests"] == 0 and completed["bytes"] == 0
    assert all(entry["state"] == "succeeded" for entry in completed["entries"].values())
    lock_id = completed["result"]["lock_id"]
    original = cli("source-replay", store.root, ["collection", "replay", lock_id])
    bundle = run_path / "collection.ssdu.zip"
    exported = cli("export", store.root, ["collection", "export", lock_id, "--output", bundle])
    imported = cli("import", fresh, ["collection", "import", bundle], forbid_source=True)
    replayed = cli(
        "destination-replay", fresh, ["collection", "replay", lock_id], forbid_source=True
    )
    assert original == replayed
    fresh_plan = cli(
        "destination-plan", fresh, ["retrieval", "plan", spec_path, "--offline"], forbid_source=True
    )
    fresh_queued = cli(
        "destination-submit",
        fresh,
        ["retrieval", "submit", fresh_plan["plan_id"], "--idempotency-key", f"{key}-fresh"],
        forbid_source=True,
    )
    assert fresh_queued["status"] == "queued" and fresh_queued["job_id"] != job_id
    fresh_worked = cli(
        "destination-work", fresh, ["jobs", "work", fresh_queued["job_id"]], forbid_source=True
    )
    assert fresh_worked["status"] == "succeeded"
    fresh_completed = cli(
        "destination-show-completed",
        fresh,
        ["jobs", "show", fresh_queued["job_id"]],
        forbid_source=True,
    )
    assert fresh_completed["status"] == "succeeded"
    assert fresh_completed["requests"] == fresh_completed["bytes"] == 0
    assert fresh_completed["result"]["lock_id"] == lock_id
    assert all(entry["state"] == "succeeded" for entry in fresh_completed["entries"].values())
    reacquired = cli(
        "destination-job-replay", fresh, ["collection", "replay", lock_id], forbid_source=True
    )
    assert reacquired == original
    health = cli("verify", fresh, ["cache", "--verify"], forbid_source=True)
    assert health["verified_count"] == len(exported["snapshot_ids"])
    intake = child(
        "intake",
        INTAKE,
        [
            fresh,
            imported["catalogue"],
            json.dumps(sorted({entry["snapshot_id"] for entry in completed["entries"].values()})),
        ],
        forbid_source=True,
    )
    assert intake["verified"]
    assert all(store.verify(snapshot_id)["files"] == files for snapshot_id, files in before.items())
    assert not guard_log.read_text(), "A guarded operation attempted network or source-store access"
    report = {
        "collection": completed["result"]["collection"],
        "plan_id": plan_id,
        "job_id": job_id,
        "status": completed["status"],
        "provider_requests": completed["requests"],
        "provider_bytes": completed["bytes"],
        "lock_id": lock_id,
        "entry_rows": {
            name: {"total": result["total_rows"], "returned": len(result["rows"])}
            for name, result in replayed["entries"].items()
        },
        "bundle": str(bundle),
        "bundle_sha256": checksum(bundle),
        "fresh_store": str(fresh),
        "fresh_plan_id": fresh_plan["plan_id"],
        "fresh_job_id": fresh_completed["job_id"],
        "fresh_job_status": fresh_completed["status"],
        "fresh_provider_requests": fresh_completed["requests"],
        "fresh_provider_bytes": fresh_completed["bytes"],
        "fresh_job_reuses_imported_lock": True,
        "catalogue": imported["catalogue"],
        "snapshot_count_including_parents": len(exported["snapshot_ids"]),
        "intake_snapshot_count": len(intake["rows"]),
        "separate_processes": processes,
        "queued_job_survived_process_restart": True,
        "completed_job_survived_process_restart": True,
        "idempotent_submission": True,
        "replay_equal": True,
        "intake_roundtrip": "passed",
        "intake_read_policy": "serial Parquet verification; Arrow CPU/I/O pools bounded to 1",
        "existing_snapshot_hashes_unchanged": True,
        "network_guard": "HttpClient.request, DNS resolution and socket connection methods",
        "destination_guard": "Store.__init__ rejects the source store; not filesystem isolation",
        "guard_attempts": 0,
    }
    (run_path / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    (base / "latest-result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("data"))
    parser.add_argument(
        "--spec", type=Path, default=Path(__file__).with_name("social-sciences.collection.yaml")
    )
    parser.add_argument("--output", type=Path, help="Parent directory for unique acceptance runs")
    args = parser.parse_args()
    print(json.dumps(run(args.store, args.spec, args.output), indent=2))


if __name__ == "__main__":
    main()
