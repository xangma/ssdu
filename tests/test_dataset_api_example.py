"""The common dataset tutorial proves interface parity and isolated offline replay."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from examples.dataset_api import fixture_manifest, run

ROOT = Path(__file__).resolve().parents[1]


def test_common_dataset_api_example_runs_all_interfaces_and_offline_replay(tmp_path):
    output = tmp_path / "evidence"
    process = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "dataset_api.py"), "--output-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    receipt = json.loads((output / "result.json").read_bytes())
    assert json.loads(process.stdout) == receipt
    assert receipt["verified"] and receipt["fresh_store_verified"]
    assert receipt["fixture_kind"] == "synthetic_teaching_fixtures"
    assert receipt["interfaces"] == ["python", "cli", "rest", "mcp"]
    assert receipt["api_parity_comparisons"] == 90
    assert {name: item["profile"] for name, item in receipt["datasets"].items()} == {
        "nomis": "observations",
        "local_csv": "records",
        "local_parquet": "records",
    }
    assert receipt["datasets"]["nomis"]["contract_version"] == "1.0.0"
    assert receipt["datasets"]["local_csv"]["contract_version"] == "1.1.0"
    assert receipt["datasets"]["local_parquet"]["contract_version"] == "1.1.0"
    assert receipt["fixture_http_requests"] > 0
    assert receipt["provider_network_requests"] == receipt["network_attempts"] == 0
    assert receipt["live_rate_settings_unchanged"]
    destination = receipt["destination"]
    assert destination["network_attempts"] == destination["original_input_attempts"] == 0
    assert destination["original_paths_unavailable"]
    assert destination["dataset_hashes_equal"] and destination["queries_equal"]
    assert destination["metadata_equal"] and destination["collection_replay_equal"]
    assert (
        len({receipt["pid"], receipt["cli_pid"], destination["pid"], destination["cli_pid"]}) == 4
    )
    assert destination["pid"] != os.getpid()
    assert not (output / "source").exists()
    assert not (output / "original-inputs").exists()
    assert (output / "retained-original-store").is_dir()
    assert (output / "retained-original-inputs" / "native-records.parquet").is_file()


def test_dataset_api_example_refuses_to_overwrite_evidence(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("Keep acceptance evidence")
    with pytest.raises(FileExistsError):
        run(tmp_path)
    assert marker.read_text() == "Keep acceptance evidence"


def test_dataset_api_fixture_provenance_is_hashed_and_explicitly_invented():
    manifest = fixture_manifest()
    assert len(manifest["files"]) == 4
    assert "independently invented" in manifest["description"]
    assert all("invented" in item["origin"] for item in manifest["files"])
    assert all(item["reference"].startswith("https://") for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_dataset_api_offline_guards_deny_original_files_and_network(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "private.csv").write_text("Do not read")
    code = r"""
import json
import socket
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dataset_api import install_offline_guards
original = Path(sys.argv[2])
events = install_offline_guards([original])
checks = [
    lambda: (original / "private.csv").read_bytes(),
    lambda: list(original.iterdir()),
    lambda: socket.getaddrinfo("example.invalid", 443),
    lambda: socket.create_connection(("example.invalid", 443)),
]
for check in checks:
    try:
        check()
    except AssertionError:
        continue
    raise AssertionError("Offline guard did not fire")
assert events == ["original_input", "original_input", "network", "network"]
print(json.dumps(events))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "examples"), str(original)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout) == ["original_input", "original_input", "network", "network"]
