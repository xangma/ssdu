"""A downloaded example builds a five-provider catalogue without live data."""

import json
import subprocess
import sys
from pathlib import Path

from ssdu.metadata_store import MetadataStore

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
    assert receipt["contract_version"] == "1.1.0"
    assert receipt["provider_network_requests"] == receipt["observation_requests"] == 0
    assert 0 < receipt["fixture_http_requests"] < 25
    assert receipt["interruption_resumed"]
    assert receipt["retained_prefix_not_refetched"]
    assert receipt["fresh_store_replayed_offline"]
    assert receipt["enriched_fields_and_evidence_replayed_offline"]
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

    store = MetadataStore(output / "fresh")
    records = {row["provider"]: row for row in store.read(receipt["snapshot_id"])["records"]}
    replay = {row["provider"]: row for row in store.read(receipt["offline_snapshot_id"])["records"]}
    descriptions = {
        "mhclg": "Independently invented community teaching publication.",
        "nisra": (
            "Invented attendance definitions for classroom exercises.\n\n"
            "Listed periods are available categories; observations are absent."
        ),
        "nomis": "Invented workshop roles for a classroom catalogue.",
        "scotland": "Invented event definitions for a metadata lesson.",
        "statswales": "Invented travel modes for a metadata lesson.",
    }
    # Scope describes the native assertion, never an inferred nonmissing extent.
    availability = {
        "mhclg": (0, "provider_declared", None, True),
        "nisra": (1, None, "dimension_categories", True),
        "nomis": (1, None, "declared_dimension", False),
        "scotland": (0, None, None, True),
        "statswales": (0, None, "provider_declared", False),
    }
    for provider, (measures, spatial, temporal, licence) in availability.items():
        assert receipt["normalized_field_availability"][provider] == {
            "description": True,
            "measure_count": measures,
            "spatial_scope": spatial,
            "temporal_scope": temporal,
            "licence_asserted": licence,
            "metadata_url": True,
            "data_access": "not_asserted",
        }
        row = records[provider]
        assert row["description"] == descriptions[provider]
        assert row["access"]["data_access"] == "not_asserted"
        for field in (
            "native_id",
            "description",
            "measures",
            "coverage",
            "licence",
            "access",
            "mapping_version",
            "captured_at",
            "last_checked_at",
            "provider_modified_at",
        ):
            assert replay[provider].get(field) == row.get(field)
        mapping = row["source_metadata"]["mapping"]
        assert replay[provider]["source_metadata"]["mapping"] == mapping
        assert mapping["version"] == row["mapping_version"] == "1.2.0"
        assert all(
            item["source_path"]
            and item["resource_paths"]
            and set(item["resource_paths"]).issubset(row["raw_resources"])
            for item in mapping["evidence"]
        )
        if provider in {"mhclg", "nisra", "scotland"}:
            assert row["provider_modified_at"] == "2026-01-01T00:00:00Z"

    assert records["nomis"]["measures"] == [
        {"id": "OBS_VALUE", "label": "OBS_VALUE", "unit": "People"}
    ]
    assert records["nomis"]["coverage"] == {
        "temporal": {
            "dimensions": [{"id": "time", "label": "TIME"}],
            "scope": "declared_dimension",
        }
    }
    assert records["nomis"]["licence"] is records["statswales"]["licence"] is None
    assert records["nisra"]["measures"] == [
        {"id": "COUNT", "label": "Invented attendees", "unit": {"label": "People"}}
    ]
    assert records["nisra"]["coverage"] == {
        "spatial": None,
        "temporal": {
            "dimensions": [{"id": "TLIST(A1)", "label": "Year"}],
            "periods": [{"id": "2023", "label": "2023"}, {"id": "2024", "label": "2024"}],
            "scope": "dimension_categories",
        },
    }
    assert records["nisra"]["licence"] == "https://example.invalid/ssdu/invented-cube-terms"
    assert records["statswales"]["measures"] == records["mhclg"]["measures"] == []
    assert records["statswales"]["coverage"] == {
        "temporal": {"start": "2023-01-01", "end": "2024-12-31", "scope": "provider_declared"}
    }
    publication_coverage = records["mhclg"]["coverage"]
    assert set(publication_coverage) == {"spatial"}
    assert publication_coverage["spatial"]["areas"] == [
        {"id": "invented-region", "label": "Invented Teaching Region"}
    ]
    assert publication_coverage["spatial"]["basis"] == "publication_applicability"
    assert (
        "does not establish the geographic extent of observations"
        in (publication_coverage["spatial"]["note"])
    )
    assert records["mhclg"]["licence"] == (
        "https://example.invalid/ssdu/invented-publication-terms"
    )
    assert records["scotland"]["measures"] == []
    assert records["scotland"]["coverage"] is None
    assert records["scotland"]["licence"] == "https://example.invalid/ssdu/invented-fixtures"
    assert records["scotland"]["native_id"].startswith("ckan:")
    assert records["scotland"]["source_metadata"]["descriptor"]["capabilities"]["metadata_only"]
    expected_native_paths = {
        "mhclg": {"/description", "/details/licence"},
        "nisra": {"/note/0", "/note/1", "/extension/copyright/href", "/role/time"},
        "nomis": {
            "/structure/keyfamilies/keyfamily/0/description/value",
            "/structure/keyfamilies/keyfamily/0/components/primarymeasure/conceptref",
            "/structure/keyfamilies/keyfamily/0/components/timedimension/conceptref",
        },
        "scotland": {"/result/description"},
        "statswales": {
            "/published_revision/metadata/0/summary",
            "/published_revision/coverage_start_date",
            "/published_revision/coverage_end_date",
        },
    }
    for provider, paths in expected_native_paths.items():
        assert paths.issubset(
            {
                item["source_path"]
                for item in records[provider]["source_metadata"]["mapping"]["evidence"]
            }
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
