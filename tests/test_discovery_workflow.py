"""Metadata updates must preserve the held-out experiment and declared unknowns."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from ssdu.discovery_benchmark import _canonical, check_coverage, load_bundle, run_benchmark
from ssdu.discovery_review import build_review_queue, diagnose_discovery
from ssdu.discovery_workflow import apply_enrichment, compare_bundles


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _freeze(root, *, configuration=None, query_vectors=None):
    corpus = json.loads((root / "corpus.json").read_bytes())["documents"]
    questions = json.loads((root / "questions.json").read_bytes())["questions"]
    np.savez(
        root / "embeddings.npz",
        doc_ids=np.array([row["id"] for row in corpus]),
        query_ids=np.array([row["id"] for row in questions]),
        document_vectors=np.array([[1, 0], [0, 1]], dtype=np.float32),
        query_vectors=np.array(query_vectors or [[1, 0], [1, 0]], dtype=np.float32),
    )
    files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.name != "manifest.json"
    }
    _write(
        root / "manifest.json",
        {
            "schema_version": 1,
            "files": files,
            "configuration": configuration
            or {"search_text_version": "1", "metric_cutoffs": [5, 10], "rrf_k": 60},
            "model": {
                "id": "artificial-test-vectors",
                "revision": "fixture",
                "asset_hashes": {},
                "dimensions": 2,
                "input_hashes": {name: files[name] for name in ("corpus.json", "questions.json")},
            },
        },
    )


@pytest.fixture
def workflow_inputs(tmp_path):
    root = tmp_path / "baseline"
    root.mkdir()
    source = {
        "id": "paper",
        "title": "A study using local labour statistics",
        "url": "https://example.org/study",
        "type": "publication",
        "published_at": "2021-01-01",
    }
    documents = [
        {
            "id": identifier,
            "provider": "nomis",
            "native_id": identifier,
            "title": title,
            "description": "",
            "variables": [],
            "concepts": ["unemployment"],
            "coverage": {
                "countries": ["England"],
                "places": [],
                "geography_levels": [],
                "geography_vintages": ["2011"],
                "period_start": None,
                "period_end": None,
                "release": "2011",
            },
            "access": "open",
            "source_urls": [source["url"]],
            "provenance": {"evidence_ids": [source["id"]]},
            "references": [source],
        }
        for identifier, title in (
            ("NM_1_1", "Unemployment statistics"),
            ("NM_2_1", "Historic labour data"),
        )
    ]
    queries = [
        {
            "id": identifier,
            "text": "Portsmouth unemployment 2019",
            "origin": "fixture",
            "split": split,
            "evidence_ids": ["paper"],
            "references": [source],
            "constraints": {"place": "Portsmouth", "period_start": 2019},
            "relevance": {"NM_1_1": 3},
            "expected_suitable": [],
            "required_groups": [],
            "suitability_judged": False,
        }
        for identifier, split in (("dev-question", "dev"), ("test-question", "test"))
    ]
    _write(root / "corpus.json", {"schema_version": 1, "documents": documents})
    _write(root / "questions.json", {"schema_version": 1, "questions": queries})
    _write(root / "evidence.json", {"schema_version": 1, "sources": [source]})
    _write(root / "derivation.json", {"method": "fixture"})
    _write(root / "review.json", {"independent_human_judgments": 0})
    _freeze(root)
    provider_source = {
        "id": "provider-metadata",
        "title": "Official dataset definition",
        "url": "https://www.nomisweb.co.uk/api/v01/dataset/NM_1_1/def.sdmx.json",
        "type": "provider_metadata",
        "published_at": None,
    }
    capture = {
        "snapshot_id": "a" * 64,
        "plan": {
            "selection_split": "dev",
            "benchmark": {
                "manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
            },
        },
        "sources": [provider_source],
        "updates": [
            {
                "dataset_id": "NM_1_1",
                "provider": "nomis",
                "native_id": "NM_1_1",
                "base_metadata_sha256": _digest(documents[0]),
                "fields": {
                    "description": "Official observation period and dimension definitions",
                    "variables": ["Employment status", "Geography"],
                    "coverage": {
                        "period_start": 2010,
                        "period_end": 2021,
                        "geography_levels": ["local_authority"],
                    },
                },
                "source_ids": [provider_source["id"]],
                "references": [provider_source],
                "field_provenance": {
                    "coverage.period_start": {
                        "source_id": provider_source["id"],
                        "pointer": "/period/start",
                    },
                },
                "provider_revision": {"revision": "current-metadata"},
                "limitations": ["No observation data or exhaustive place coverage captured."],
            }
        ],
    }
    return root, capture


def test_apply_compare_and_review_preserve_the_frozen_experiment(workflow_inputs, tmp_path):
    root, capture = workflow_inputs
    original = {path.name: path.read_bytes() for path in root.iterdir()}
    output = tmp_path / "enriched"
    application = apply_enrichment(root, capture, output)
    assert application["updated_datasets"] == 1
    assert application["embedding_preparation_required"] is True
    assert {path.name: path.read_bytes() for path in root.iterdir()} == original
    for name in ("questions.json", "review.json"):
        assert (output / name).read_bytes() == original[name]
    _freeze(output)
    before, after = load_bundle(root), load_bundle(output)
    updated = next(row for row in after.documents if row["id"] == "NM_1_1")
    assert updated["coverage"]["release"] == "2011"
    assert updated["coverage"]["geography_vintages"] == ["2011"]
    assert updated["coverage"]["places"] == []
    assert updated["provenance"]["enrichment_snapshot_id"] == capture["snapshot_id"]
    assert {row["id"] for row in updated["references"]} == {"paper", "provider-metadata"}
    # A current metadata revision cannot turn a historical extract into a 2021 release.
    assert check_coverage(updated, {"release": "2021"})["status"] == "ineligible"
    assert check_coverage(updated, {"place": "Portsmouth"})["status"] == "unknown"
    before_report, after_report = run_benchmark(before), run_benchmark(after)
    comparison = compare_bundles(
        before, after, before_report=before_report, after_report=after_report
    )
    assert comparison["questions_labels_splits_unchanged"] is True
    assert comparison["query_vectors_unchanged"] is True
    assert comparison["independent_human_judgments"] == 0
    metric = comparison["methods"]["hybrid"]["by_split"]["test"]["ndcg@5"]
    assert metric["delta"] == 0.0
    assert metric["before"]["questions"] == metric["after"]["questions"] == 1
    assert {row["split"] for row in comparison["coverage_changes"]} == {"dev", "test"}
    queue = build_review_queue(before, before_report, question_limit=30)
    assert queue == build_review_queue(before, before_report, question_limit=30)
    assert queue["counts"]["human_judgments"] == 0
    assert queue["counts"]["pending_pairs"] == len(queue["rows"])
    assert all(row["question_references"] and row["dataset_references"] for row in queue["rows"])
    assert diagnose_discovery(after, after_report)["counts"]["unjudged_suitability_questions"] == 2


def test_conflicting_known_coverage_is_retained_and_reported(workflow_inputs, tmp_path):
    root, capture = workflow_inputs
    capture["updates"][0]["fields"]["coverage"]["countries"] = ["Wales"]
    output = tmp_path / "enriched"
    result = apply_enrichment(root, capture, output)
    doc = json.loads((output / "corpus.json").read_bytes())["documents"][0]
    report = json.loads((output / "derivation.json").read_bytes())["metadata_enrichment"]
    assert result["conflicts"] == 1
    assert doc["coverage"]["countries"] == ["England"]
    assert report["conflicts"][0]["retained"] == ["England"]
    assert report["conflicts"][0]["observed"] == ["Wales"]
    assert report["conflicts"][0]["references"]


@pytest.mark.parametrize(
    "field,value",
    [("release", "2021"), ("geography_vintages", ["2021"]), ("places", ["Portsmouth"])],
)
def test_enrichment_refuses_historical_substitution_or_partial_place_lists(
    workflow_inputs, tmp_path, field, value
):
    root, capture = workflow_inputs
    capture["updates"][0]["fields"]["coverage"][field] = value
    with pytest.raises(ValueError, match="cannot substitute releases, vintages or place coverage"):
        apply_enrichment(root, capture, tmp_path / "enriched")
    assert not (tmp_path / "enriched").exists()


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("baseline", "different baseline"),
        ("test-selection", "development questions"),
        ("identity", "dataset identity"),
        ("stale-document", "planned baseline"),
        ("unknown-source", "retained sources"),
        ("duplicate", "Duplicate or unknown"),
    ],
)
def test_enrichment_binding_rejects_mismatched_or_unattributed_updates(
    workflow_inputs, tmp_path, mutation, error
):
    root, capture = workflow_inputs
    update = capture["updates"][0]
    if mutation == "baseline":
        capture["plan"]["benchmark"]["manifest_sha256"] = "b" * 64
    elif mutation == "test-selection":
        capture["plan"]["selection_split"] = "test"
    elif mutation == "identity":
        update["native_id"] = "NM_999_1"
    elif mutation == "stale-document":
        update["base_metadata_sha256"] = "b" * 64
    elif mutation == "unknown-source":
        update["source_ids"] = ["missing-source"]
    elif mutation == "duplicate":
        capture["updates"].append(copy.deepcopy(update))
    with pytest.raises(ValueError, match=error):
        apply_enrichment(root, capture, tmp_path / "enriched")
    assert not (tmp_path / "enriched").exists()


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("labels", "byte-identical questions"),
        ("split", "byte-identical questions"),
        ("query-vectors", "unchanged query vectors"),
        ("configuration", "unchanged search configuration"),
        ("model", "same pinned embedding model"),
        ("report", "does not belong"),
    ],
)
def test_comparison_refuses_changes_to_experimental_controls(
    workflow_inputs, tmp_path, mutation, error
):
    root, capture = workflow_inputs
    output = tmp_path / "enriched"
    apply_enrichment(root, capture, output)
    queries = json.loads((output / "questions.json").read_bytes())
    if mutation == "labels":
        queries["questions"][0]["relevance"]["NM_1_1"] = 1
        _write(output / "questions.json", queries)
    elif mutation == "split":
        queries["questions"][0]["split"] = "test"
        _write(output / "questions.json", queries)
    _freeze(
        output,
        configuration={"search_text_version": "1", "metric_cutoffs": [5, 10], "rrf_k": 61}
        if mutation == "configuration"
        else None,
        query_vectors=[[0, 1], [1, 0]] if mutation == "query-vectors" else None,
    )
    if mutation == "model":
        manifest = json.loads((output / "manifest.json").read_bytes())
        manifest["model"]["revision"] = "another-model-revision"
        _write(output / "manifest.json", manifest)
    before_report = run_benchmark(output) if mutation == "report" else run_benchmark(root)
    with pytest.raises(ValueError, match=error):
        compare_bundles(
            root, output, before_report=before_report, after_report=run_benchmark(output)
        )


def test_portable_transfer_copies_only_verified_manifest_inventory(workflow_inputs, tmp_path):
    root, _ = workflow_inputs
    example = Path(__file__).resolve().parents[1] / "examples/discovery_review/replay.py"
    if not example.is_file():
        pytest.skip("Source-only publication excludes the private discovery replay example")
    spec = importlib.util.spec_from_file_location("discovery_replay_fixture", example)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (root / "local-unverified-notes.json").write_text("not part of the freeze")
    copied = tmp_path / "transferred"
    hashes = replay.copy_benchmark(root, copied)
    assert "local-unverified-notes.json" not in hashes
    assert not (copied / "local-unverified-notes.json").exists()
    assert load_bundle(copied).manifest == load_bundle(root).manifest
    (root / "corpus.json").write_text("tampered")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        replay.copy_benchmark(root, tmp_path / "tampered-transfer")
