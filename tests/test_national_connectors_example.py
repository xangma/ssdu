"""The standalone national connector tutorial uses only invented responses."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_national_connectors_example_transfers_and_replays_offline(tmp_path):
    # A downloaded copy must run without the repository's example modules or fixtures.
    script = tmp_path / "national_connectors.py"
    script.write_bytes((ROOT / "examples/national_connectors.py").read_bytes())
    output = tmp_path / "example"
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
    assert receipt["fixture_kind"] == "independently_invented_teaching_data"
    assert receipt["provider_network_requests"] == 0
    assert receipt["scotland_service"] == "retained_legacy_linked_data"
    assert receipt["metadata_replayed_offline"]
    assert receipt["fresh_store_replayed_offline"]
    assert receipt["original_store_path_unavailable"]
    assert not (output / "source").exists()
    assert (output / "fresh").is_dir()
    assert (output / "invented-national-connectors.ssdu.zip").is_file()
    assert set(receipt["datasets"]) == {"nisra", "scotland"}
    assert all(item["rows"] == 1 for item in receipt["datasets"].values())
    ni = receipt["datasets"]["nisra"]
    scotland = receipt["datasets"]["scotland"]
    assert ni["request"]["params"] == {"selectors": {"TLIST(A1)": ["2024"]}}
    assert scotland["request"]["params"] == {
        "dimensions": {
            "http://purl.org/linked-data/sdmx/2009/dimension#refPeriod": [
                "http://reference.data.gov.uk/id/year/2024"
            ]
        }
    }
    assert all(
        item["protocol_reference"].startswith("https://") for item in receipt["datasets"].values()
    )

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
