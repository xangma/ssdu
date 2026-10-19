"""The downloaded deep-metadata example transfers a verified offline graph."""

import json
import subprocess
import sys
from pathlib import Path

from ssdu.metadata_graph import GRAPH_CONTRACT_VERSION
from ssdu.metadata_graph_store import MetadataGraphStore

ROOT = Path(__file__).resolve().parents[1]


def test_downloaded_graph_example_replays_in_a_fresh_offline_process(tmp_path):
    script = tmp_path / "metadata_graph.py"
    script.write_bytes((ROOT / "examples/metadata_graph.py").read_bytes())
    output = tmp_path / "capture"
    result = subprocess.run(
        [sys.executable, str(script), "--output-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == json.loads((output / "result.json").read_text())
    assert receipt["status"] == "passed"
    assert receipt["graph_contract_version"] == GRAPH_CONTRACT_VERSION
    assert receipt["network_attempts"] == receipt["observation_requests"] == 0
    assert receipt["fixture_http_requests"] > 0
    for check in (
        "interruption_resumed",
        "retained_prefix_not_refetched",
        "cached_capture_zero_http",
        "metadata_only",
        "fresh_store_verified",
        "offline_replay_equal",
        "source_timestamps_preserved",
        "original_store_path_unavailable",
    ):
        assert receipt[check], check
    assert not (output / "source").exists()
    assert (output / "invented-metadata-graph.ssdu-metadata.zip").is_file()
    store = MetadataGraphStore(output / "fresh")
    store.verify(receipt["offline_snapshot_id"])
    page = store.entities(receipt["offline_snapshot_id"], limit=100)
    assert page["next_offset"] is None
    rows = page["items"]
    assert {"dataset", "table", "variable", "question", "code_list", "code"} <= {
        row["kind"] for row in rows
    }
    assert receipt["entity_counts"] == {
        kind: sum(row["kind"] == kind for row in rows) for kind in {row["kind"] for row in rows}
    }
    datasets = [row for row in rows if row["kind"] == "dataset"]
    assert {row["native_id"] for row in datasets} == {"001"}
    assert {row["origin"] for row in datasets} == {"02", "03"}
    assert len({row["id"] for row in datasets}) == 2
    codes = [row for row in rows if row["kind"] == "code"]
    assert {row["attributes"]["value"] for row in codes} == {"01", "99"}
    missing = [row for row in codes if row["attributes"]["isMissing"]]
    assert len(missing) == 2
    assert all(
        row["attributes"]["value"] == "99" and row["label"] == "Not stated" for row in missing
    )
    assert all(
        ref["captured_at"] and ref["checked_at"] for row in rows for ref in row["provenance"]
    )

    marker = output / "keep.txt"
    marker.write_text("Keep this capture")
    repeated = subprocess.run(
        [sys.executable, str(script), "--output-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert repeated.returncode != 0
    assert "Choose an absent or empty output directory" in repeated.stderr
    assert marker.read_text() == "Keep this capture"
