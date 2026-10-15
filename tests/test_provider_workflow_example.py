"""The provider tutorial uses unrelated invented tables and replays a transferred lock."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from examples.provider_workflow import PUBLICATIONS, run

ROOT = Path(__file__).resolve().parents[1]


def test_provider_workflow_example_replays_two_datasets_offline(tmp_path):
    output = tmp_path / "example"
    process = subprocess.run(
        [sys.executable, str(ROOT / "examples/provider_workflow.py"), "--output-dir", str(output)],
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
    assert receipt["fixture_kind"] == "independently_invented_teaching_data"
    assert receipt["provider_network_requests"] == 0
    # One catalogue, two initial descriptions, two CSVs and two release rechecks.
    assert receipt["fixture_http_requests"] == 7
    assert receipt["metadata_replayed_offline"] and receipt["fresh_store_replayed_offline"]
    assert receipt["original_store_path_unavailable"]
    assert not (output / "source").exists()
    assert (output / "fresh").is_dir()
    assert (output / "invented-publications.ssdu.zip").is_file()
    assert set(receipt["datasets"]) == set(PUBLICATIONS)
    for dataset_id, item in PUBLICATIONS.items():
        assert receipt["datasets"][dataset_id]["rows"] == item["expected"]


def test_provider_workflow_refuses_to_overwrite_a_run(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("Keep this evidence")
    with pytest.raises(FileExistsError):
        run(tmp_path)
    assert marker.read_text() == "Keep this evidence"
