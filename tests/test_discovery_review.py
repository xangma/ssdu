"""Review pools are human-ready artifacts, not independently reviewed labels."""

import copy
import hashlib
import json

import numpy as np
import pytest

from ssdu.discovery_benchmark import BenchmarkBundle, run_benchmark
from ssdu.discovery_review import (
    ReviewError,
    build_review_queue,
    diagnose_discovery,
    import_judgments,
)


def _sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture
def experiment(tmp_path):
    documents = [
        {
            "id": identifier,
            "provider": provider,
            "native_id": native,
            "title": title,
            "description": "Employment statistics",
            "variables": ["employment"],
            "concepts": ["employment"],
            "coverage": coverage,
            "access": "open",
            "source_urls": ["https://example.org/metadata"],
            "provenance": {"evidence_ids": ["metadata"]},
        }
        for identifier, provider, native, title, coverage in (
            (
                "england",
                "nomis",
                "NM_1_1",
                "Employment in Portsmouth England",
                {"countries": ["England"], "places": ["Portsmouth"]},
            ),
            (
                "wales",
                "statswales",
                "welsh-table",
                "Employment in Wales",
                {"countries": ["Wales"], "places": ["Wales"]},
            ),
            ("historic", "deprivation", "release-2019", "Historical context", {}),
        )
    ]
    questions = []
    for index in range(6):
        questions.append(
            {
                "id": f"q{index}",
                "text": "Employment in Portsmouth" if index < 3 else "Employment in Wales",
                "split": "dev" if index % 2 else "test",
                "origin": "publication" if index < 3 else "curated",
                "paper_id": f"paper-{index // 2}",
                "identity_levels": ["specific_release" if index < 3 else "family"],
                "constraints": {
                    "country": "England" if index < 3 else "Wales",
                    "place": "Portsmouth" if index < 3 else "Wales",
                },
                "evidence_ids": ["paper"],
                "relevance": {"historic": 3} if index < 3 else {"wales": 3},
                "expected_suitable": [],
                "required_groups": [],
                "suitability_judged": False,
                "label_scope": "Known evidenced use; unlabelled is not a verified negative.",
            }
        )
    bundle = BenchmarkBundle(
        root=tmp_path,
        manifest={"schema_version": 1, "files": {}, "model": {"name": "fixture"}},
        documents=documents,
        questions=questions,
        evidence=[
            {
                "id": "paper",
                "title": "An employment study",
                "url": "https://example.org/paper",
                "type": "publication_reference",
            },
            {
                "id": "metadata",
                "title": "Provider metadata",
                "url": "https://example.org/metadata",
                "type": "provider_metadata",
            },
        ],
        document_vectors=np.array([[1, 0], [0.8, 0.2], [0, 1]], dtype=float),
        query_vectors=np.array([[1, 0]] * 6, dtype=float),
        lexical_weights=(10, 8, 4, 3, 1, 2, 1),
        rrf_k=60,
        cutoffs=(1, 2),
    )
    (tmp_path / "manifest.json").write_text(json.dumps(bundle.manifest, indent=2), encoding="utf-8")
    return bundle, run_benchmark(bundle)


def _queue(experiment, **kwargs):
    bundle, report = experiment
    return build_review_queue(bundle, report, pool_depth=1, **kwargs)


def _judgment(queue, row=None):
    return {
        "row_id": (row or queue["rows"][0])["id"],
        "reviewer": {"type": "human", "name": "Independent reviewer"},
        "reviewed_at": "2026-10-14T12:00:00+01:00",
        "reason": "The paper identifies this release; geography remains unknown.",
        "references": ["paper", "metadata"],
        "relevance": 3,
        "identity": "confirmed",
        "suitability": "unknown",
    }


def _submission(queue, rows):
    return {
        "queue_sha256": queue["queue_sha256"],
        "benchmark_id": queue["binding"]["benchmark_id"],
        "judgments": rows,
    }


def test_pool_is_deduplicated_stratified_and_retains_evidenced_misses(experiment):
    queue = _queue(experiment, question_limit=5)
    assert queue == _queue(experiment, question_limit=5)
    assert len(queue["questions"]) == 5
    assert queue["selection"]["policy_version"] == 2
    assert "portsmouth_questions" not in queue["counts"]
    assert all("portsmouth" not in row for row in queue["questions"])
    assert {row["split"] for row in queue["questions"]} == {"dev", "test"}
    assert {row["origin"] for row in queue["questions"]} == {"publication", "curated"}
    assert len({(row["question_id"], row["dataset_id"]) for row in queue["rows"]}) == len(
        queue["rows"]
    )
    assert any(row["added_evidenced_target"] for row in queue["pooling"])
    assert queue["counts"]["human_judgments"] == 0
    assert (
        queue["binding"]["manifest_sha256"]
        == hashlib.sha256((experiment[0].root / "manifest.json").read_bytes()).hexdigest()
    )
    assert queue["binding"]["benchmark_id"] == _sha(experiment[0].manifest)
    assert queue["binding"]["benchmark_id"] != queue["binding"]["manifest_sha256"]
    for row in queue["rows"]:
        assert row["human_judgment"] is None
        assert row["review_status"] == "pending"
        assert row["question_references"] and row["dataset_references"]
        assert row["coverage_check"]["checks"]
        assert "method_ranks" not in row
        assert "prior_relevance_grade" not in row
    # The default is a limit, so a smaller experiment remains usable.
    assert _queue(experiment)["counts"]["questions"] == 6


def test_neutral_selection_does_not_prefer_a_city_mentioned_in_text(experiment):
    bundle = copy.deepcopy(experiment[0])
    template = bundle.questions[0]
    bundle.questions = [
        {**copy.deepcopy(template), "id": identifier, "text": text, "constraints": {}}
        for identifier, text in (("a", "Employment"), ("z", "Employment in Portsmouth"))
    ]
    bundle.query_vectors = bundle.query_vectors[:2]
    neutral = build_review_queue(bundle, question_limit=1, pool_depth=1)
    assert neutral["questions"][0]["id"] == "a"
    assert "portsmouth" not in neutral["questions"][0]
    legacy = build_review_queue(
        bundle, question_limit=1, pool_depth=1, selection_policy="legacy-v1"
    )
    assert legacy["questions"][0]["id"] == "z"
    assert legacy["questions"][0]["portsmouth"]
    assert "policy_version" not in legacy["selection"]
    assert "policy_version" not in diagnose_discovery(bundle, selection_policy="legacy-v1")
    assert diagnose_discovery(bundle)["policy_version"] == 2


def test_optional_cohorts_bind_known_selected_question_ids(experiment):
    cohorts = {"local-study": ["q0", "q1"], "empty": []}
    queue = _queue(experiment, question_limit=1, cohorts=cohorts)
    selected = {row["id"] for row in queue["questions"]}
    assert queue["cohorts"] == {"local-study": sorted(selected & {"q0", "q1"}), "empty": []}
    assert queue["queue_sha256"] != _queue(experiment, question_limit=1)["queue_sha256"]
    for invalid in ({"bad": ["missing"]}, {"bad": ["q0", "q0"]}, {"": []}, []):
        with pytest.raises(ReviewError, match="Cohorts"):
            _queue(experiment, cohorts=invalid)
    with pytest.raises(ReviewError, match="Cohorts"):
        _queue(experiment, cohorts=cohorts, selection_policy="legacy-v1")


def test_provided_report_is_reused_and_report_binding_is_checked(experiment, monkeypatch):
    bundle, report = experiment
    monkeypatch.setattr(
        "ssdu.discovery_review.run_benchmark",
        lambda *_: pytest.fail("The frozen report must be reused"),
    )
    assert _queue(experiment)["rows"]
    assert diagnose_discovery(bundle, report)["questions"]
    stale = copy.deepcopy(report)
    stale["benchmark_id"] = "other"
    with pytest.raises(ReviewError, match="binding"):
        build_review_queue(bundle, stale, pool_depth=1)
    changed = copy.deepcopy(report)
    changed["methods"]["lexical"]["questions"][0]["ranking"][0]["id"] = "wales"
    with pytest.raises(ReviewError, match="hash"):
        diagnose_discovery(bundle, changed)


def test_no_report_runs_the_same_frozen_experiment(experiment):
    bundle, report = experiment
    assert build_review_queue(bundle, pool_depth=1) == build_review_queue(
        bundle, report, pool_depth=1
    )


def test_diagnostics_preserve_unknowns_and_incomplete_label_scope(experiment):
    bundle, report = experiment
    diagnostics = diagnose_discovery(bundle, report)
    assert diagnostics["counts"]["questions"] == 6
    assert diagnostics["counts"]["unjudged_suitability_questions"] == 6
    first = diagnostics["questions"][0]
    assert first["independent_review_pending"]
    assert not first["suitability_judged"]
    for method in first["methods"].values():
        assert "not a verified negative" in first["label_scope"]
        miss = method["evidenced_target_misses"][0]
        assert miss["dataset_id"] == "historic"
        assert miss["status"] == "unknown"
        assert miss["question_references"] and miss["dataset_references"]
        assert method["unlabelled_result_count"] > 0
    historic = next(gap for gap in diagnostics["metadata_gaps"] if gap["dataset_id"] == "historic")
    assert historic["unknown_fields"] == ["country", "place"]
    assert historic["dev_question_count"] and historic["test_question_count"]
    assert len(historic["questions"]) == 3  # Deduplicate occurrences across methods.
    assert all(len(row["methods"]) == 3 for row in historic["questions"])
    assert diagnostics["methods"]["semantic"]["known_violation_results"] > 0


def test_partial_human_import_keeps_pending_rows_and_never_creates_global_qrels(experiment):
    queue = _queue(experiment)
    original = copy.deepcopy(queue)
    imported = import_judgments(queue, _submission(queue, [_judgment(queue)]))
    assert queue == original
    assert imported["counts"]["human_judgments"] == 1
    assert imported["counts"]["pending_pairs"] == len(queue["rows"]) - 1
    assert imported["rows"][0]["human_judgment"]["references"][0]["title"]
    assert all(not item["global_qrels_complete"] for item in imported["question_progress"])
    assert any(row["human_judgment"] is None for row in imported["rows"])
    assert "declared" in imported["reviewer_verification"]
    completed = import_judgments(
        queue,
        _submission(queue, [_judgment(queue, row) for row in queue["rows"]]),
    )
    assert completed["counts"]["complete_question_pools"] == 6
    assert all(not item["global_qrels_complete"] for item in completed["question_progress"])


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("reviewer", {"type": "agent", "name": "Codex"}, "human"),
        ("reviewer", {"type": "human", "name": "Reviewer", "automated": True}, "human"),
        ("generated_by", "language model", "human"),
        ("relevance", True, "integer"),
        ("relevance", 4, "integer"),
        ("identity", "maybe", "identity"),
        ("identity", [], "identity"),
        ("suitability", "suitable", "suitability"),
        ("reason", "", "reason"),
        ("reviewed_at", "2026-10-14", "timezone"),
        ("reviewed_at", "nonsense", "timestamp"),
        ("references", [], "references"),
        ("references", ["unknown-paper"], "Unknown reference"),
        ("row_id", "unknown-row", "Unknown judgment"),
    ],
)
def test_judgment_validation(experiment, field, value, message):
    queue = _queue(experiment)
    judgment = _judgment(queue)
    judgment[field] = value
    with pytest.raises(ReviewError, match=message):
        import_judgments(queue, _submission(queue, [judgment]))


def test_duplicate_stale_and_changed_queue_are_rejected(experiment):
    queue = _queue(experiment)
    judgment = _judgment(queue)
    with pytest.raises(ReviewError, match="Duplicate"):
        import_judgments(queue, _submission(queue, [judgment, judgment]))
    submission = _submission(queue, [judgment])
    submission["queue_sha256"] = "stale"
    with pytest.raises(ReviewError, match="stale"):
        import_judgments(queue, submission)
    queue["rows"][0]["dataset"]["title"] = "changed after review"
    with pytest.raises(ReviewError, match="content hash"):
        import_judgments(queue, _submission(queue, []))


def test_new_citations_are_explicit_and_existing_citations_cannot_be_rewritten(experiment):
    queue = _queue(experiment)
    judgment = _judgment(queue)
    judgment["references"] = [
        {"id": "new-paper", "title": "Release-specific evidence", "url": "https://example.org/new"}
    ]
    assert import_judgments(queue, _submission(queue, [judgment]))["counts"]["human_judgments"] == 1
    judgment["references"][0]["url"] = "file:///private/file"
    with pytest.raises(ReviewError, match="HTTP"):
        import_judgments(queue, _submission(queue, [judgment]))
    judgment["references"] = [{"id": "paper", "title": "Changed paper"}]
    with pytest.raises(ReviewError, match="Changed known"):
        import_judgments(queue, _submission(queue, [judgment]))


@pytest.mark.parametrize(
    "kwargs",
    [{"question_limit": 0}, {"question_limit": True}, {"pool_depth": 3}, {"pool_depth": False}],
)
def test_invalid_limits(experiment, kwargs):
    bundle, report = experiment
    with pytest.raises(ReviewError):
        build_review_queue(bundle, report, **kwargs)


def test_report_with_rehashed_duplicate_results_is_rejected(experiment):
    bundle, report = experiment
    changed = copy.deepcopy(report)
    ranking = changed["methods"]["semantic"]["questions"][0]["ranking"]
    ranking[1] = copy.deepcopy(ranking[0])
    changed["replay_sha256"] = _sha(
        {
            key: value
            for key, value in changed.items()
            if key not in {"replay_sha256", "environment"}
        }
    )
    with pytest.raises(ReviewError, match="Duplicate result"):
        diagnose_discovery(bundle, changed)


def test_manifest_file_changed_after_loading_is_rejected(experiment):
    bundle, report = experiment
    (bundle.root / "manifest.json").write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(ReviewError, match="manifest"):
        build_review_queue(bundle, report, pool_depth=1)
