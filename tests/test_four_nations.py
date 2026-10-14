"""Four-nation teaching collection runs through real acquisition and replay paths."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from examples.four_nations import DESTINATION, fixture_manifest, run

ROOT = Path(__file__).resolve().parents[1]


def environment():
    return {
        **os.environ,
        "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }


def test_four_nations_acquisition_resume_and_fresh_store_offline(tmp_path):
    output = tmp_path / "evidence"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "four_nations.py"),
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        env=environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=150,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    receipt = json.loads((output / "result.json").read_bytes())
    assert json.loads(process.stdout) == receipt
    assert receipt["verified"] and receipt["fresh_store_verified"]
    assert receipt["fixture_kind"] == "synthetic_teaching_fixtures"
    assert set(receipt["nations"]) == {"england", "wales", "scotland", "northern_ireland"}
    assert {nation: item["provider"] for nation, item in receipt["nations"].items()} == {
        "england": "deprivation",
        "wales": "statswales",
        "scotland": "scotland",
        "northern_ireland": "nisra",
    }
    assert receipt["worker_restarted"]
    assert receipt["completed_entries_reused"] == ["england"]
    assert receipt["retained_responses_reused"] == 2
    assert receipt["job_status"] == "succeeded"
    assert receipt["job_requests"] > 0
    assert receipt["network_attempts"] == receipt["destination"]["network_attempts"] == 0
    assert receipt["destination"]["source_store_attempts"] == 0
    assert receipt["destination"]["pid"] != os.getpid()
    assert receipt["live_rate_settings_unchanged"]
    assert all(item["cached_requests"] == 0 for item in receipt["nations"].values())
    assert all(
        item["normalized_equal"] and item["raw_equal"] and item["intake_equal"]
        for item in receipt["destination"]["providers"].values()
    )


def test_four_nation_output_never_overwrites_existing_files(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("Keep existing acceptance evidence")
    with pytest.raises(FileExistsError):
        run(tmp_path)
    assert marker.read_text() == "Keep existing acceptance evidence"


def test_four_nation_fixture_references_and_hashes_are_retained():
    manifest = fixture_manifest()
    assert manifest["kind"] == "synthetic_teaching_fixtures"
    assert set(manifest["nations"]) == {"england", "wales", "scotland", "northern_ireland"}
    assert all(item["reference"].startswith("https://") for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    assert all(item["path"] for item in manifest["files"])


def test_destination_guards_block_network_and_original_store_reads(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "secret.txt").write_text("Must not be read")
    code = (
        DESTINATION.split("\nimport intake\n", 1)[0]
        + r"""
checks = [
    lambda: (source / "secret.txt").read_text(),
    lambda: list(source.iterdir()),
    lambda: socket.getaddrinfo("example.invalid", 443),
    lambda: socket.create_connection(("example.invalid", 443)),
]
for check in checks:
    try:
        check()
    except AssertionError:
        continue
    raise AssertionError("An offline guard did not fire")
assert events == ["source_store", "source_store", "network", "network"], events
print("guards verified")
"""
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(source), "unused", "unused", "unused"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert process.stdout.strip() == "guards verified"
