"""One dataset API for invented provider observations and local CSV/Parquet records.

Run ``python examples/dataset_api.py --output-dir NEW_DIR``. Python, CLI, REST
and MCP read the same verified snapshots. A fresh process repeats those reads
after bundle import with network, original store and original input reads denied.
All input values are invented; no provider statistics or live requests are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import anyio
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient
from mcp import Client

from ssdu import DatasetQuerySpec, LocalImportSpec, Service, Store
from ssdu.api import create_app
from ssdu.bundles import export_bundle, import_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.mcp_server import create_mcp_server
from ssdu.store import canonical

if __package__:
    from .dataset_contract import fixture_client, no_network, request_for
    from .four_nations import table_hash
else:
    from dataset_contract import fixture_client, no_network, request_for
    from four_nations import table_hash

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "dataset-api"


def fixture_manifest() -> dict:
    """Verify the invented local inputs and the reused invented Nomis responses."""
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    if manifest["kind"] != "synthetic_teaching_fixtures":
        raise ValueError("Expected independently invented teaching fixtures")
    for item in manifest["files"]:
        path = (HERE / item["path"]).resolve()
        if not path.is_relative_to(HERE) or not item["reference"].startswith("https://"):
            raise ValueError("Invalid fixture provenance")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Fixture hash mismatch: {item['path']}")
    return manifest


def install_offline_guards(forbidden_roots) -> list[str]:
    """Deny Python file access to original inputs and all socket connections."""
    roots = [Path(value).resolve() for value in forbidden_roots]
    events = []

    def denied_network(*args, **kwargs):
        events.append("network")
        raise AssertionError("Dataset API offline replay attempted network access")

    def guard(event, args):
        if event in {"open", "os.listdir", "os.scandir"} and args:
            value = args[0]
            if isinstance(value, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(value)).resolve()
                if any(path == root or root in path.parents for root in roots):
                    events.append("original_input")
                    raise AssertionError("Offline replay attempted an original input read")

    sys.addaudithook(guard)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, denied_network)
    socket.create_connection = denied_network
    socket.socket.connect = denied_network
    socket.socket.connect_ex = denied_network
    HttpClient.request = denied_network
    return events


CLI = r"""
import contextlib
import io
import json
import os
import sys
sys.path.insert(0, sys.argv[1])
from dataset_api import install_offline_guards
events = install_offline_guards(json.loads(sys.argv[2]))
from ssdu.cli import main
results = []
for arguments in json.loads(sys.argv[3]):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        main(arguments)
    results.append(json.loads(output.getvalue()))
assert events == [], events
print(json.dumps({"results": results, "pid": os.getpid(), "network_attempts": 0}))
"""


def read_interfaces(store: Store, tasks: dict, evidence: Path, *, forbidden_roots=()) -> dict:
    """Compare actual CLI parsing, REST routing and MCP protocol reads with Python."""
    evidence.mkdir(parents=True, exist_ok=False)
    service = Service(store.root, index_store=False)
    python, rest, calls, cli_arguments = {}, {}, [], []
    with TestClient(create_app(service=service)) as api:
        for name, task in tasks.items():
            snapshot_id = task["snapshot_id"]
            base = f"/snapshots/{snapshot_id}/dataset"
            python[name] = {"describe": service.describe_snapshot(snapshot_id), "queries": []}
            response = api.get(base)
            assert response.status_code == 200, response.text
            rest[name] = {"describe": response.json(), "queries": []}
            calls.append((name, "describe", "describe_snapshot", {"snapshot_id": snapshot_id}))
            cli_arguments.append(["--store", str(store.root), "dataset", "describe", snapshot_id])
            if task.get("dimension"):
                dimension = task["dimension"]
                python[name]["dimensions"] = service.list_snapshot_dimension_values(
                    snapshot_id, dimension
                )
                response = api.get(f"{base}/dimensions/{dimension}")
                assert response.status_code == 200, response.text
                rest[name]["dimensions"] = response.json()
                calls.append(
                    (
                        name,
                        "dimensions",
                        "list_snapshot_dimension_values",
                        {"snapshot_id": snapshot_id, "dimension": dimension},
                    )
                )
                cli_arguments.append(
                    ["--store", str(store.root), "dataset", "dimensions", snapshot_id, dimension]
                )
            for index, query in enumerate(task["queries"]):
                python[name]["queries"].append(
                    service.query_dataset(snapshot_id, DatasetQuerySpec.model_validate(query))
                )
                response = api.post(f"{base}/query", json=query)
                assert response.status_code == 200, response.text
                rest[name]["queries"].append(response.json())
                calls.append(
                    (name, index, "query_dataset", {"snapshot_id": snapshot_id, "query": query})
                )
                query_path = evidence / f"{name}-query-{index}.json"
                query_path.write_bytes(canonical(query) + b"\n")
                cli_arguments.append(
                    [
                        "--store",
                        str(store.root),
                        "dataset",
                        "query",
                        snapshot_id,
                        "--query",
                        str(query_path),
                    ]
                )
    assert rest == python
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI,
            str(HERE),
            json.dumps([str(path) for path in forbidden_roots]),
            json.dumps(cli_arguments),
        ],
        cwd=evidence,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    cli = json.loads(process.stdout)
    expected = [
        python[name]["queries"][key] if isinstance(key, int) else python[name][key]
        for name, key, _tool, _arguments in calls
    ]
    assert cli["results"] == expected

    async def mcp_reads():
        async with Client(create_mcp_server(service=service)) as client:
            for (_name, _key, tool, arguments), value in zip(calls, expected, strict=True):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result.content
                assert result.structured_content == value

    anyio.run(mcp_reads)
    return {
        "results": python,
        "comparisons": len(calls) * 3,
        "cli_pid": cli["pid"],
        "cli_network_attempts": cli["network_attempts"],
        "interfaces": ["python", "cli", "rest", "mcp"],
    }


def make_parquet(path: Path) -> None:
    """Create invented native values; the repository contains no binary input."""
    pq.write_table(
        pa.table(
            {
                "unit_code": pa.array(["001", "002", "003"], type=pa.string()),
                "as_of": pa.array([date(2025, 1, 1)] * 3, type=pa.date32()),
                "amount": pa.array(
                    [Decimal("10.50"), None, Decimal("0.00")], type=pa.decimal128(12, 2)
                ),
                "eligible": pa.array([True, False, None], type=pa.bool_()),
            }
        ),
        path,
    )


def parquet_spec() -> dict:
    return {
        "dataset_id": "synthetic-native-records",
        "title": "Invented native Parquet records",
        "description": "Invented decimal, date and nullable boolean values; not statistics.",
        "licence": "MIT; independently invented SSDU teaching fixture",
        "citation": "SSDU synthetic dataset API example, runtime Parquet fixture version 1",
        "row_grain": "One invented service unit on the stated date",
        "primary_key": ["unit_code"],
        "columns": {
            "unit_code": {"role": "identifier", "nullable": False, "label": "Invented unit"},
            "as_of": {"label": "Invented reporting date", "nullable": False},
            "amount": {"label": "Invented amount", "unit": "GBP", "nullable": True},
            "eligible": {"label": "Invented eligibility", "nullable": True},
        },
    }


def task_for(dataset, *, dimension=None) -> dict:
    key = "geography" if dataset.contract["profile"] == "observations" else "unit_code"
    value = "K02000001" if key == "geography" else "001"
    return {
        "snapshot_id": dataset.snapshot_id,
        "dimension": dimension,
        "queries": [
            {"limit": 100},
            {"filters": [{"column": key, "value": value}], "limit": 100},
            {"table": "source", "limit": 100},
        ],
    }


def assert_semantics(results: dict) -> None:
    """Different profiles retain meanings without pretending that they are comparable."""
    nomis, csv, parquet = (results[name] for name in ("nomis", "local_csv", "local_parquet"))
    assert nomis["describe"]["profile"] == "observations"
    assert nomis["describe"]["contract_version"] == "1.0.0"
    assert nomis["describe"]["default_table"] == "observations"
    rows = nomis["queries"][0]["rows"]
    assert [row["value"] for row in rows] == [1.0, None, 0.0]
    assert [row["value_raw"] for row in rows] == ["001", "..", "0"]
    assert [row["status"] for row in rows] == ["A", "S", "A"]
    assert [row["measure"] for row in rows] == ["MEASURES=001"] * 3
    # A general source annotation is not sufficient to bind a per-measure unit.
    assert [row["unit"] for row in rows] == [None] * 3
    assert any(
        diagnostic["code"] == "unknown_unit"
        for diagnostic in nomis["queries"][0]["quality"]["diagnostics"]
    )
    assert nomis["dimensions"]["items"] == [{"value": "K02000001", "label": "United Kingdom"}]
    for result in (csv, parquet):
        assert result["describe"]["profile"] == "records"
        assert result["describe"]["contract_version"] == "1.1.0"
        assert result["describe"]["default_table"] == "records"
        descriptor = result["describe"]["descriptor"]
        assert descriptor["primary_key"] == ["unit_code"]
        assert descriptor["row_grain"]
        assert descriptor["dimensions"] == []  # Identifiers do not imply geography.
        assert [row["unit_code"] for row in result["queries"][0]["rows"]] == [
            "001",
            "002",
            "003",
        ]
        assert result["queries"][1]["rows"][0]["unit_code"] == "001"
        assert len(result["queries"][1]["rows"]) == 1
        assert result["queries"][2]["table"] == "source"
        assert result["dimensions"]["items"] == [
            {"value": code, "label": code} for code in ("001", "002", "003")
        ]
    csv_rows = csv["queries"][0]["rows"]
    records_table = next(
        table for table in csv["describe"]["descriptor"]["tables"] if table["name"] == "records"
    )
    columns = {column["name"]: column for column in records_table["columns"]}
    assert columns["unit_code"]["role"] == "identifier"
    assert columns["status"]["missing_values"] == {
        "missing": "No estimate supplied; not a suppressed value"
    }
    assert [row["estimate"] for row in csv_rows] == [12.5, None, 0.0]
    assert [row["status"] for row in csv_rows] == ["reported", "missing", "reported"]
    assert csv["queries"][0]["units"]["columns"] == {"estimate": "persons"}
    parquet_rows = parquet["queries"][0]["rows"]
    assert [row["amount"] for row in parquet_rows] == ["10.50", None, "0.00"]
    assert [row["as_of"] for row in parquet_rows] == ["2025-01-01"] * 3
    assert [row["eligible"] for row in parquet_rows] == [True, False, None]
    assert parquet["queries"][0]["units"]["columns"] == {"amount": "GBP"}


def offline_replay(expected_path: str | Path) -> dict:
    """Import into an empty store, with all original acquisition inputs unavailable."""
    expected_path = Path(expected_path)
    expected = json.loads(expected_path.read_bytes())
    assert all(not Path(root).exists() for root in expected["forbidden_roots"][:2])
    events = install_offline_guards(expected["forbidden_roots"])
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    store = Store(expected["fresh_store"])
    imported = import_bundle(store, expected["bundle"])
    parity = read_interfaces(
        store,
        expected["tasks"],
        expected_path.parent / "replay-interface-evidence",
        forbidden_roots=expected["forbidden_roots"],
    )
    assert_semantics(parity["results"])
    assert parity["results"] == expected["results"]
    for name, task in expected["tasks"].items():
        dataset = store.open_dataset(task["snapshot_id"])
        assert dataset.contract == expected["datasets"][name]["descriptor"]
        assert {name: table_hash(dataset.to_arrow(name)) for name in dataset.table_names} == (
            expected["datasets"][name]["table_hashes"]
        )
        assert store.verify(task["snapshot_id"])["files"] == expected["datasets"][name]["files"]
    replayed = Collections(store).replay(expected["lock_id"])
    assert hashlib.sha256(canonical(replayed)).hexdigest() == expected["replay_sha256"]
    assert imported["bundle_id"] == expected["bundle_id"]
    assert events == [], events
    return {
        "verified": True,
        "pid": os.getpid(),
        "network_attempts": 0,
        "original_input_attempts": 0,
        "original_paths_unavailable": True,
        "dataset_hashes_equal": True,
        "queries_equal": True,
        "metadata_equal": True,
        "collection_replay_equal": True,
        "interfaces": parity["interfaces"],
        "comparisons": parity["comparisons"],
        "cli_pid": parity["cli_pid"],
    }


def run(output_dir: str | Path) -> dict:
    """Acquire, import, read through four interfaces, lock, transfer and replay."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = fixture_manifest()
    inputs = output / "original-inputs"
    inputs.mkdir()
    csv_path = inputs / "services.csv"
    csv_path.write_bytes((FIXTURES / "local-services.csv").read_bytes())
    parquet_path = inputs / "native-records.parquet"
    make_parquet(parquet_path)
    parquet_sha256 = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    store = Store(output / "source")
    rate_keys = ("SSDU_RATE_DB", "SSDU_RATE_INTERVAL_SECONDS")
    original_rate_settings = {key: os.environ.get(key) for key in rate_keys}
    with no_network() as network_attempts:
        with fixture_client("nomis") as client:
            observations = store.fetch_dataset(request_for("nomis"), client=client)
            fixture_requests = client.attempts
            assert fixture_requests == len(client.fixture_responses.calls) > 0

        def forbid_http(*args, **kwargs):
            raise AssertionError("Retained dataset reads attempted a provider request")

        with patch.object(HttpClient, "request", forbid_http):
            datasets = {
                "nomis": observations,
                "local_csv": store.import_dataset(
                    csv_path,
                    LocalImportSpec.model_validate(
                        json.loads((FIXTURES / "local-import.json").read_bytes())
                    ),
                ),
                "local_parquet": store.import_dataset(parquet_path, parquet_spec()),
            }
            cached = store.fetch_dataset(request_for("nomis"), offline=True)
            assert cached.snapshot_id == observations.snapshot_id
            tasks = {
                name: task_for(dataset, dimension="geography" if name == "nomis" else "unit_code")
                for name, dataset in datasets.items()
            }
            parity = read_interfaces(store, tasks, output / "source-interface-evidence")
            assert_semantics(parity["results"])
            lock = Collections(store).resolve(
                {
                    "name": "dataset-api-teaching-example",
                    "description": "Invented observations and local records; no cross-data joins",
                    "entries": {
                        name: {"snapshot_id": dataset.snapshot_id}
                        for name, dataset in datasets.items()
                    },
                },
                offline=True,
            )
            replayed = Collections(store).replay(lock.lock_id)
            bundle_path = output / "dataset-api.ssdu.zip"
            exported = export_bundle(store, lock, bundle_path)
            expected = {
                "results": parity["results"],
                "tasks": tasks,
                "datasets": {
                    name: {
                        "descriptor": dataset.contract,
                        "table_hashes": {
                            table: table_hash(dataset.to_arrow(table))
                            for table in dataset.table_names
                        },
                        "files": store.verify(dataset.snapshot_id)["files"],
                    }
                    for name, dataset in datasets.items()
                },
                "forbidden_roots": [
                    str(store.root),
                    str(inputs),
                    str(output / "retained-original-store"),
                    str(output / "retained-original-inputs"),
                    str(HERE / "fixtures"),
                ],
                "fresh_store": str(output / "fresh"),
                "bundle": str(bundle_path),
                "bundle_id": exported["bundle_id"],
                "lock_id": lock.lock_id,
                "replay_sha256": hashlib.sha256(canonical(replayed)).hexdigest(),
            }
            expected_path = output / "expected.json"
            expected_path.write_bytes(canonical(expected) + b"\n")
            # Native readers may bypass Python audit hooks. Moving the two private
            # directories also makes their original absolute paths unavailable.
            store.root.rename(output / "retained-original-store")
            inputs.rename(output / "retained-original-inputs")
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,sys; sys.path.insert(0,sys.argv[1]); "
                        "from dataset_api import offline_replay; "
                        "print(json.dumps(offline_replay(sys.argv[2])))"
                    ),
                    str(HERE),
                    str(expected_path),
                ],
                cwd=output,
                capture_output=True,
                text=True,
                check=False,
                timeout=150,
            )
            assert process.returncode == 0, process.stdout + process.stderr
            destination = json.loads(process.stdout)
            assert destination["pid"] != os.getpid()
            assert destination["cli_pid"] != parity["cli_pid"]
        assert network_attempts == []
    assert original_rate_settings == {key: os.environ.get(key) for key in rate_keys}
    result = {
        "verified": True,
        "fixture_kind": manifest["kind"],
        "fixture_files": manifest["files"],
        "runtime_parquet": {
            "origin": "Independently invented by make_parquet in examples/dataset_api.py",
            "sha256": parquet_sha256,
        },
        "datasets": {
            name: {
                "snapshot_id": dataset.snapshot_id,
                "profile": dataset.contract["profile"],
                "contract_version": dataset.contract["contract_version"],
                "table_hashes": expected["datasets"][name]["table_hashes"],
            }
            for name, dataset in datasets.items()
        },
        "interfaces": parity["interfaces"],
        "api_parity_comparisons": parity["comparisons"] + destination["comparisons"],
        "collection_lock_id": lock.lock_id,
        "bundle_id": exported["bundle_id"],
        "fresh_store_verified": destination["verified"],
        "destination": destination,
        "pid": os.getpid(),
        "cli_pid": parity["cli_pid"],
        "fixture_http_requests": fixture_requests,
        "provider_network_requests": 0,
        "network_attempts": len(network_attempts),
        "live_rate_settings_unchanged": True,
    }
    (output / "result.json").write_bytes(canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data/validation/dataset-api"))
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
