"""A downloaded example builds a five-provider catalogue without live data."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_metadata_sync_example_resumes_and_transfers_offline(tmp_path):
    # The download must work without repository example modules or test fixtures.
    script = tmp_path / "metadata_sync.py"
    script.write_bytes((ROOT / "examples/metadata_sync.py").read_bytes())
    output = tmp_path / "catalogue"
    process = subprocess.run(
        [sys.executable, str(script), "--output-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    receipt = json.loads(process.stdout)
    assert json.loads((output / "result.json").read_bytes()) == receipt
    assert receipt["status"] == "passed"
    assert receipt["fixture_kind"] == "independently_invented_teaching_metadata"
    assert receipt["providers"] == ["mhclg", "nisra", "nomis", "scotland", "statswales"]
    assert receipt["records"] == receipt["search_results"] == 5
    assert receipt["contract_version"] == "1.0.0"
    assert receipt["provider_network_requests"] == receipt["observation_requests"] == 0
    assert receipt["fixture_http_requests"] > 0
    assert receipt["interruption_resumed"]
    assert receipt["retained_prefix_not_refetched"]
    assert receipt["fresh_store_replayed_offline"]
    assert receipt["original_store_path_unavailable"]
    assert len(receipt["provider_reports"]) == len(receipt["freshness"]) == 5
    assert all(row["completeness"]["status"] == "complete" for row in receipt["provider_reports"])
    assert all(row["completeness"]["catalogue_complete"] for row in receipt["provider_reports"])
    assert all(row["completeness"]["descriptions_complete"] for row in receipt["provider_reports"])
    assert all(row["completeness"]["codes_complete"] for row in receipt["provider_reports"])
    assert all(row["freshness"]["state"] == "fresh" for row in receipt["freshness"])
    assert all(row["freshness"]["last_checked_at"] for row in receipt["freshness"])
    assert not (output / "source").exists()
    assert (output / "fresh").is_dir()
    assert (output / "invented-catalogue.ssdu-metadata.zip").is_file()

    marker = output / "keep.txt"
    marker.write_text("Keep this run")
    repeat = subprocess.run(
        [sys.executable, str(script), "--output-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert repeat.returncode != 0
    assert "Choose an absent or empty output directory" in repeat.stderr
    assert marker.read_text() == "Keep this run"
