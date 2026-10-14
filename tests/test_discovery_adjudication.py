"""Agent adjudication is bound, evidence-checked and distinct from human review."""

import copy
import hashlib
import json
import math

import pytest

from ssdu.discovery_adjudication import (
    AdjudicationError,
    build_adjudication,
    evaluate_adjudication,
    evaluate_independent,
)
from ssdu.discovery_review import _sha


def _rehash(queue):
    queue["queue_sha256"] = _sha({k: v for k, v in queue.items() if k != "queue_sha256"})
    return queue


@pytest.fixture
def inputs(tmp_path):
    text = "The 2019 resource covers Portsmouth. Newer releases are distinct."
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ref = {
        "path": "source.txt",
        "sha256": digest,
        "url": "https://example.org/source",
        "selector": "first sentence",
        "excerpt": "The 2019 resource covers Portsmouth.",
    }
    queue = {
        "schema_version": 1,
        "artifact_type": "discovery_review_queue",
        "binding": {"benchmark_id": "b" * 64},
        "questions": [{"id": "q1", "portsmouth": True}, {"id": "q2", "portsmouth": False}],
        "rows": [
            {"id": identifier, "question_id": question, "dataset_id": identifier}
            for identifier, question in (("a", "q1"), ("b", "q1"), ("c", "q1"), ("d", "q2"))
        ],
        "pooling": [
            {
                "row_id": identifier,
                "question_id": question,
                "dataset_id": identifier,
                "prior_relevance_grade": prior,
                "prior_suitable": suitable,
                "method_ranks": {
                    method: {"rank": rank, "score": 1 / rank} for method, rank in ranks.items()
                },
            }
            for identifier, question, prior, suitable, ranks in (
                ("a", "q1", 0, False, {"lexical": 1, "semantic": 2, "hybrid": 1}),
                ("b", "q1", 2, True, {"lexical": 2, "semantic": 1, "hybrid": 2}),
                ("c", "q1", None, None, {"lexical": 3, "semantic": 3, "hybrid": 3}),
                ("d", "q2", None, None, {"lexical": 1, "semantic": 1, "hybrid": 1}),
            )
        ],
    }
    _rehash(queue)
    binding = {
        "queue_sha256": queue["queue_sha256"],
        "benchmark_id": queue["binding"]["benchmark_id"],
    }
    independent = {
        "schema_version": 1,
        **binding,
        "reviewer": {"type": "agent", "name": "Independent fixture"},
        "judgments": [
            {
                "row_id": identifier,
                "question_id": question,
                "dataset_id": identifier,
                "relevance": grade,
                "analysis_suitability": suitability,
                "study_dataset_identity": "family",
                "reason": "Independent source assessment.",
                "suitability_reason": "Coverage is independently assessed.",
                "field_constraint_evidence": {"release": {"status": "unknown"}},
                "inspected_references": [
                    {
                        "retained_path": "source.txt",
                        "sha256": digest,
                        "url": ref["url"],
                        "selector": ref["selector"],
                        "excerpt": ref["excerpt"],
                        "inspected": True,
                        "source_kind": "retained_provider_body",
                    }
                ],
            }
            for identifier, question, grade, suitability in (
                ("a", "q1", 3, "eligible"),
                ("b", "q1", 2, "unknown"),
                ("c", "q1", 0, "ineligible"),
                ("d", "q2", 0, "unknown"),
            )
        ],
    }
    submission = {
        "schema_version": 1,
        **binding,
        "reviewer": {"type": "agent", "name": "Adjudicating fixture"},
        "decisions": [
            {
                "row_id": identifier,
                "question_id": "q1",
                "dataset_id": identifier,
                "prior_grade": prior,
                "independent_grade": grade,
                "final_grade": final,
                "reason": "The cited source resolves the disagreement.",
                "references": [copy.deepcopy(ref)],
            }
            for identifier, prior, grade, final in (("a", 0, 3, 3), ("b", 2, 2, 2))
        ],
        "additional_decisions": [],
        "question_decisions": [
            {
                "question_id": "q1",
                "reason": "The requested release is explicit.",
                "references": [copy.deepcopy(ref)],
                "annotation": "A source-backed note.",
            }
        ],
    }
    return queue, independent, submission, tmp_path


def _build(inputs):
    queue, independent, submission, root = inputs
    return build_adjudication(queue, independent, [submission], evidence_root=root)


def test_complete_bound_deterministic_and_nonmutating(inputs):
    before = copy.deepcopy(inputs[:3])
    artifact = _build(inputs)
    assert inputs[:3] == before
    assert artifact == _build(inputs)
    assert artifact == json.loads(json.dumps(artifact))
    assert artifact["human_review_status"] == "pending"
    assert artifact["counts"]["required_disagreements"] == 2
    assert artifact["counts"]["adjudicated_pairs"] == 2
    assert artifact["counts"]["human_judgments"] == 0
    assert artifact["counts"]["remaining_disagreements"] == 0
    assert artifact["judgments"][2]["assessment_status"] == "retained_independent_assessment"
    assert len(artifact["required_sources"]) == 1
    assert artifact["binding"]["independent_sha256"] == _sha(inputs[1])
    assert artifact["binding"]["submissions_sha256"] == [_sha(inputs[2])]
    assert artifact["question_decisions"][0]["annotation"] == "A source-backed note."


def test_complementary_question_notes_keep_each_submission_provenance(inputs):
    queue, independent, first, root = inputs
    second = copy.deepcopy(first)
    second["reviewer"]["name"] = "Other collection component"
    second["decisions"] = [first["decisions"].pop()]
    second["question_decisions"][0]["reason"] = "A separate component has different coverage."
    artifact = build_adjudication(queue, independent, [first, second], evidence_root=root)
    notes = artifact["question_decisions"]
    assert len(notes) == 2
    assert {note["submission_sha256"] for note in notes} == {_sha(first), _sha(second)}
    first["question_decisions"].append(copy.deepcopy(first["question_decisions"][0]))
    with pytest.raises(AdjudicationError, match="Duplicate question"):
        build_adjudication(queue, independent, [first, second], evidence_root=root)


@pytest.mark.parametrize("target", ["queue", "independent", "submission"])
def test_stale_binding_rejected(inputs, target):
    queue, independent, submission, _ = inputs
    if target == "queue":
        queue["questions"][0]["portsmouth"] = False
    elif target == "independent":
        independent["benchmark_id"] = "c" * 64
    else:
        submission["queue_sha256"] = "c" * 64
    with pytest.raises(AdjudicationError, match="binding|hash"):
        _build(inputs)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "duplicate",
        "unknown",
        "stale_prior",
        "stale_independent",
        "bad_grade",
        "human",
        "additional_required",
        "bad_identity",
        "bad_suitability",
    ],
)
def test_invalid_or_incomplete_decisions_rejected(inputs, kind):
    submission = inputs[2]
    decision = submission["decisions"][0]
    if kind == "missing":
        submission["decisions"].pop()
    elif kind == "duplicate":
        submission["decisions"].append(copy.deepcopy(decision))
    elif kind == "unknown":
        decision["row_id"] = "unknown"
    elif kind == "stale_prior":
        decision["prior_grade"] = 1
    elif kind == "stale_independent":
        decision["independent_grade"] = 1
    elif kind == "bad_grade":
        decision["final_grade"] = True
    elif kind == "human":
        submission["reviewer"]["type"] = "human"
    elif kind == "additional_required":
        submission["additional_decisions"].append(submission["decisions"].pop())
    elif kind == "bad_identity":
        decision["final_identity"] = "exact_original_extract"
    else:
        decision["final_suitability"] = "probably"
    with pytest.raises(AdjudicationError):
        _build(inputs)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "wrong_pair", "human"])
def test_independent_must_cover_exact_pool(inputs, kind):
    independent = inputs[1]
    if kind == "missing":
        independent["judgments"].pop()
    elif kind == "duplicate":
        independent["judgments"].append(copy.deepcopy(independent["judgments"][0]))
    elif kind == "wrong_pair":
        independent["judgments"][0]["dataset_id"] = "other"
    else:
        independent["reviewer"]["type"] = "human"
    with pytest.raises(AdjudicationError):
        _build(inputs)


@pytest.mark.parametrize("kind", ["hash", "excerpt", "path", "symlink", "missing_url"])
def test_new_sources_require_safe_files_hashes_and_literal_excerpts(inputs, kind):
    reference = inputs[2]["decisions"][0]["references"][0]
    if kind == "hash":
        reference["sha256"] = "a" * 64
    elif kind == "excerpt":
        reference["excerpt"] = "A plausible sentence that is not actually in this file."
    elif kind == "path":
        reference["path"] = "../source.txt"
    elif kind == "symlink":
        (inputs[3] / "link.txt").symlink_to(inputs[3] / "source.txt")
        reference["path"] = "link.txt"
    else:
        reference["url"] = ""
    with pytest.raises(AdjudicationError):
        _build(inputs)


def test_changed_suitability_never_reuses_old_checks_as_final_evidence(inputs):
    decision = inputs[2]["decisions"][1]
    decision["final_suitability"] = "eligible"
    with pytest.raises(AdjudicationError, match="changed suitability reason"):
        _build(inputs)
    decision["suitability_reason"] = "The literal source authenticates coverage."
    final = _build(inputs)["judgments"][1]
    assert final["analysis_suitability"] == "eligible"
    assert "field_constraint_evidence" not in final
    assert (
        final["independent_assessment"]["field_constraint_evidence"]["release"]["status"]
        == "unknown"
    )


def test_inherited_transformed_selections_are_not_claimed_literal(inputs):
    reference = inputs[1]["judgments"][2]["inspected_references"][0]
    reference["excerpt"] = "A normalized structured selection."
    final = _build(inputs)["judgments"][2]
    assert final["references"][0]["excerpt_status"] == "inherited_selection_not_literal"
    assert "excerpt" not in final["references"][0]
    assert final["independent_inspected_references"][0]["excerpt"] == reference["excerpt"]
    reference["sha256"] = "a" * 64
    with pytest.raises(AdjudicationError, match="hash"):
        _build(inputs)


def test_additional_decisions_are_explicit_and_support_null_prior_grade(inputs):
    submission = inputs[2]
    extra = copy.deepcopy(submission["decisions"][0])
    extra.update(row_id="c", dataset_id="c", prior_grade=None, independent_grade=0, final_grade=1)
    submission["additional_decisions"].append(extra)
    artifact = _build(inputs)
    assert artifact["counts"]["additional_decisions"] == 1
    assert artifact["judgments"][2]["relevance"] == 1
    assert artifact["judgments"][2]["adjudication"]["additional"]


def test_metrics_hand_calculation_unknowns_and_null_denominators(inputs):
    artifact = _build(inputs)
    evaluation = evaluate_adjudication(inputs[0], artifact)
    baseline = evaluate_independent(inputs[0], inputs[1])
    assert evaluation["methods"] == baseline["methods"]
    lexical = evaluation["methods"]["lexical"]
    first, second = lexical["questions"]
    assert first["metrics"]["ndcg@10"] == 1
    assert second["metrics"]["ndcg@10"] is None
    assert lexical["metrics"]["ndcg@10"] == 1
    assert lexical["denominators"]["ndcg@10"] == 1
    assert first["metrics"]["useful_precision@10"] == pytest.approx(0.2)
    assert first["metrics"]["eligible_precision@10"] == pytest.approx(0.1)
    assert first["metrics"]["eligibility_precision_known@10"] == pytest.approx(0.5)
    assert first["metrics"]["unknown_suitability_share@10"] == pytest.approx(0.1)
    assert first["metrics"]["unknown_returned_share@10"] == pytest.approx(1 / 3)
    assert second["metrics"]["eligibility_precision_known@10"] is None
    assert second["metrics"]["eligible_pool_recall@10"] is None
    assert lexical["metrics"]["eligible_pool_recall@10"] == 1
    assert lexical["portsmouth"]["questions"] == 1
    semantic = evaluation["methods"]["semantic"]["questions"][0]["metrics"]
    expected = (3 + 7 / math.log2(3)) / (7 + 3 / math.log2(3))
    assert semantic["ndcg@10"] == pytest.approx(expected)
    assert evaluation == evaluate_adjudication(inputs[0], json.loads(json.dumps(artifact)))


@pytest.mark.parametrize("cohorts", [None, {"local-study": ["q1"], "empty": []}])
def test_new_queue_evaluation_has_only_explicit_cohorts(inputs, cohorts):
    queue, independent, submission, _ = inputs
    queue["selection"] = {"policy_version": 2}
    for question in queue["questions"]:
        question.pop("portsmouth")
    if cohorts is not None:
        queue["cohorts"] = cohorts
    _rehash(queue)
    independent["queue_sha256"] = submission["queue_sha256"] = queue["queue_sha256"]
    evaluation = evaluate_adjudication(queue, _build(inputs))
    assert evaluation["policy_version"] == 2
    assert evaluation["methods"] == evaluate_independent(queue, independent)["methods"]
    for method in evaluation["methods"].values():
        assert "portsmouth" not in method
        assert all("portsmouth" not in row for row in method["questions"])
        if cohorts is None:
            assert "cohorts" not in method
        else:
            assert method["cohorts"]["local-study"]["questions"] == 1
            empty = method["cohorts"]["empty"]
            assert empty["questions"] == 0
            assert set(empty["metrics"].values()) == {None}
            assert set(empty["denominators"].values()) == {0}


@pytest.mark.parametrize("cohorts", [{"bad": ["missing"]}, {"bad": ["q1", "q1"]}, {"": []}])
def test_invalid_bound_cohorts_are_rejected(inputs, cohorts):
    inputs[0]["selection"] = {"policy_version": 2}
    inputs[0]["cohorts"] = cohorts
    _rehash(inputs[0])
    with pytest.raises(AdjudicationError, match="cohorts"):
        _build(inputs)


def test_tampered_final_artifact_and_duplicate_ranks_rejected(inputs):
    artifact = _build(inputs)
    artifact["judgments"][0]["relevance"] = 0
    with pytest.raises(AdjudicationError, match="hash"):
        evaluate_adjudication(inputs[0], artifact)
    inputs[0]["pooling"][1]["method_ranks"]["lexical"]["rank"] = 1
    _rehash(inputs[0])
    with pytest.raises(AdjudicationError, match="rank"):
        _build(inputs)


@pytest.mark.parametrize(
    "kind", ["queue_version", "independent_version", "submission_version", "prior_suitable"]
)
def test_boolean_versions_and_nonboolean_suitability_are_rejected(inputs, kind):
    if kind == "queue_version":
        inputs[0]["schema_version"] = True
        _rehash(inputs[0])
    elif kind == "independent_version":
        inputs[1]["schema_version"] = True
    elif kind == "submission_version":
        inputs[2]["schema_version"] = True
    else:
        inputs[0]["pooling"][0]["prior_suitable"] = 0
        _rehash(inputs[0])
    with pytest.raises(AdjudicationError):
        _build(inputs)


def test_packet_only_inherited_reference_is_truthfully_internal(inputs):
    reference = inputs[1]["judgments"][2]["inspected_references"][0]
    reference.pop("url")
    reference.pop("excerpt")
    reference["source_kind"] = "blind_candidate_assertion"
    final = _build(inputs)["judgments"][2]
    assert len(final["references"]) == 1
    assert final["references"][0]["url"] == f"urn:ssdu:source:{reference['sha256']}"
    assert final["references"][0]["source_kind"] == "blind_candidate_assertion"
    assert final["references"][0]["excerpt_status"] == "not_supplied"


def test_inherited_extracted_text_and_original_bytes_are_both_required(inputs):
    reference = inputs[1]["judgments"][2]["inspected_references"][0]
    original = inputs[3] / "original.raw"
    original.write_bytes(b"\xffBinary document bytes")
    reference["extracted_text"] = {
        "retained_path": reference["retained_path"],
        "sha256": reference["sha256"],
    }
    reference.update(
        retained_path="original.raw", sha256=hashlib.sha256(original.read_bytes()).hexdigest()
    )
    artifact = _build(inputs)
    final = artifact["judgments"][2]
    assert final["references"][0]["path"] == "source.txt"
    assert final["references"][0]["original_source"]["path"] == "original.raw"
    assert {item["path"] for item in artifact["required_sources"]} == {"source.txt", "original.raw"}
    original.write_bytes(b"changed")
    with pytest.raises(AdjudicationError, match="hash mismatch"):
        _build(inputs)
