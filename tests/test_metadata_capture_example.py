"""A downloaded full-capture example works with five synthetic catalogues offline."""

import json
import subprocess
import sys
from pathlib import Path

from ssdu.metadata_index import MetadataIndex

ROOT = Path(__file__).resolve().parents[1]


def test_downloaded_metadata_capture_transfers_complete_normalized_cache(tmp_path):
    script = tmp_path / "metadata_capture.py"
    script.write_bytes((ROOT / "examples/metadata_capture.py").read_bytes())
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
    assert receipt == json.loads((output / "result.json").read_bytes())
    assert receipt["status"] == "passed"
    assert receipt["providers"] == ["mhclg", "nisra", "nomis", "scotland", "statswales"]
    assert receipt["records"] == receipt["search_results"] == 10
    assert receipt["contract_version"] == "1.1.0"
    assert receipt["mapping_version"] == "1.2.0"
    assert receipt["provider_network_requests"] == receipt["observation_requests"] == 0
    assert receipt["interruption_resumed"] and receipt["retained_prefix_not_refetched"]
    assert receipt["fresh_store_replayed_offline"]
    assert receipt["normalized_fields_and_evidence_equal"]
    assert receipt["index_rebuilt_offline"]
    assert receipt["original_store_path_unavailable"]
    assert (output / "invented-full-catalogue.ssdu-metadata.zip").is_file()
    assert not (output / "source").exists()
    for provider, report in receipt["report"]["report"]["providers"].items():
        assert report["catalogue_records"] == report["described_records"] == 2, provider
        assert report["catalogue_complete"] and report["descriptions_complete"]
        assert report["catalogue_coverage_percent"] == report["descriptor_coverage_percent"] == 100
        assert report["all_provider_holdings_percent"] is None
        assert report["field_status_counts"]["description"]["mapped"] == 2
        assert report["raw_metadata_bytes"] > 0

    index = MetadataIndex(output / "fresh")
    publication = index.get("mhclg:ssdu-invented-rural")
    native = publication["source_metadata"]["descriptor"]
    assert not native["capabilities"]["retrieval"]
    assert publication["kind"] == "publication"
    assert any("spreadsheet" in row["format"] for row in publication["distributions"])
    scotland = index.get("scotland:ckan:00000000-0000-4000-8000-000000000006")
    assert scotland["source_metadata"]["descriptor"]["capabilities"]["metadata_only"]
    assert scotland["distributions"][0]["supported"] is False
    assert scotland["distributions"][0]["format"] == "XLSX"
    assert scotland["dimensions"] == scotland["measures"] == []
    assert scotland["publishers"] == [
        {"id": "invented-community-office", "name": "Invented community office"}
    ]
    assert scotland["languages"] == ["en"]
    assert scotland["subjects"][0]["label"] == "Community teaching"
    assert scotland["coverage"]["spatial"]["scope"] == "provider_declared"
    assert scotland["coverage"]["temporal"]["basis"] == "reference_period"
    assert scotland["field_status"]["dimensions"]["status"] == "not_provided"
    welsh = index.get("statswales:00000000-0000-4000-8000-000000000001")
    assert welsh["field_status"]["measures"]["status"] == "unmapped"
    assert welsh["field_status"]["licence"]["status"] == "not_provided"

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
