"""Lock any collection, transfer it to an empty store and verify offline replay.

Run: uv run --offline python examples/collection_transfer.py --offline
Supply --spec PATH for a different collection, including local-only snapshots.
Offline resolution requires retained inputs. Transfer always blocks networking.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import intake
import pandas as pd

from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.store import Store, checksum

GUARD = """
import socket
from pathlib import Path
from ssdu.http import HttpClient
from ssdu.store import Store
def denied(*args, **kwargs):
    raise AssertionError('Offline transfer attempted network access')
HttpClient.request = denied
socket.getaddrinfo = denied
socket.create_connection = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
original_init = Store.__init__
def init(self, root='data'):
    if Path(root).resolve() == Path(__FORBIDDEN_STORE__):
        raise AssertionError('Fresh-store replay opened the original store')
    original_init(self, root)
Store.__init__ = init
"""


def cli(store, args, env):
    result = subprocess.run(
        [sys.executable, "-m", "ssdu.cli", "--store", str(store), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"CLI failed: {args[0]}\n{result.stderr}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="data")
    parser.add_argument(
        "--spec", type=Path, default=Path(__file__).with_name("social-sciences.collection.yaml")
    )
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    store = Store(args.store)
    if args.offline:

        def denied(*a, **k):
            raise AssertionError("Offline collection resolution attempted provider HTTP")

        HttpClient.request = denied
    before = {m["product_id"]: m["files"] for m in store.list()}
    collections = Collections(store)
    lock = collections.resolve(args.spec, offline=args.offline)
    original = collections.replay(lock)
    validation = store.root / "validation"
    validation.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="collection-transfer-", dir=validation))
    fresh = run / "fresh-store"
    fresh.mkdir()
    assert not list(fresh.iterdir())
    bundle_path = run / f"{lock.spec.name}.ssdu.zip"
    with tempfile.TemporaryDirectory(prefix="ssdu-transfer-guard-") as guards:
        guard = Path(guards, "sitecustomize.py")
        env = {
            **os.environ,
            "PYTHONPATH": guards + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        guard.write_text(GUARD.replace("__FORBIDDEN_STORE__", repr(str(run / "not-a-store"))))
        exported = cli(
            store.root,
            ["collection", "export", lock.lock_id, "--output", str(bundle_path)],
            env,
        )
        guard.write_text(GUARD.replace("__FORBIDDEN_STORE__", repr(str(store.root))))
        imported = cli(fresh, ["collection", "import", str(bundle_path)], env)
        replayed = cli(fresh, ["collection", "replay", lock.lock_id], env)
        assert replayed == original
        health = cli(fresh, ["cache", "--verify"], env)
        assert health["verified_count"] == len(exported["snapshot_ids"])
    destination = Store(fresh)
    cat = intake.from_yaml_file(imported["catalogue"])
    for snapshot_id in exported["snapshot_ids"]:
        assert destination.verify(snapshot_id) == store.verify(snapshot_id)
    for entry in lock.entries.values():
        provider = destination.verify(entry.snapshot_id)["provider"]
        loaded = cat[f"{provider}_{entry.snapshot_id[:12]}"].read()
        pd.testing.assert_frame_equal(loaded, destination.replay(entry.snapshot_id))
    assert all(store.verify(snapshot_id)["files"] == files for snapshot_id, files in before.items())
    report = {
        "name": lock.spec.name,
        "lock_id": lock.lock_id,
        "bundle_id": exported["bundle_id"],
        "bundle": str(bundle_path),
        "bundle_sha256": checksum(bundle_path),
        "fresh_store": str(fresh),
        "catalogue": imported["catalogue"],
        "entries": len(lock.entries),
        "entry_rows": {
            name: {"total": result["total_rows"], "returned": len(result["rows"])}
            for name, result in original["entries"].items()
        },
        "snapshots_including_parents": len(exported["snapshot_ids"]),
        "replay_equal": replayed == original,
        "destination_source_store_open_blocked": True,
        "transfer_network_blocked": True,
        "resolution_offline": args.offline,
        "intake_roundtrip": "passed",
        "existing_snapshot_hashes_unchanged": True,
    }
    (run / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    (validation / f"{lock.spec.name}-collection-result.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
