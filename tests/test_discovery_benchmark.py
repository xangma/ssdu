"""Small artificial vectors test mathematics, not real-world model performance."""

import copy
import hashlib
import json
import shutil
import socket

import numpy as np
import pytest

from ssdu.discovery_benchmark import (
    DEFAULT_COVERAGE_POLICY,
    LEGACY_COVERAGE_POLICY,
    BenchmarkError,
    check_coverage,
    coverage_policy,
    load_bundle,
    rank,
    run_benchmark,
    searchable_text,
)


def document(identifier, title, *, countries=None, places=None, concept="unemployment"):
    return {
        "id": identifier,
        "provider": "fixture",
        "native_id": identifier,
        "title": title,
        "description": "",
        "variables": [],
        "concepts": [concept],
        "coverage": {
            "countries": countries or ["England"],
            "places": places or ["Portsmouth"],
            "geography_levels": ["local_authority"],
            "geography_vintages": ["2021"],
            "period_start": 2019,
            "period_end": 2025,
            "release": "2025",
        },
        "access": "open",
        "source_urls": ["https://example.org/source"],
        "provenance": {"evidence_ids": ["paper"]},
    }


def _save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _freeze(root, *, configuration=None):
    files = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("corpus.json", "questions.json", "evidence.json", "embeddings.npz")
    }
    _save_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "files": files,
            "model": {
                "name": "artificial-test-vectors",
                "input_hashes": {name: files[name] for name in ("corpus.json", "questions.json")},
            },
            "configuration": configuration
            or {
                "search_text_version": "1",
                "metric_cutoffs": [5, 10],
                "rrf_k": 60,
            },
        },
    )


@pytest.fixture
def bundle_dir(tmp_path):
    root = tmp_path / "frozen"
    root.mkdir()
    docs = [
        document("benefits", "People without work in Portsmouth", concept="claimant_count"),
        document("labour", "Annual Population Survey unemployment"),
        document("wales", "People without work in Wales", countries=["Wales"], places=["Wales"]),
        document("unknown", "Historic unemployment bulletin"),
    ]
    docs[3]["coverage"].update({"places": [], "period_start": None, "period_end": None})
    queries = [
        {
            "id": "paraphrase",
            "text": "People without work in Portsmouth",
            "origin": "fixture",
            "split": "test",
            "evidence_ids": ["paper"],
            "constraints": {"country": "England", "place": "E06000044", "concept": "unemployment"},
            "relevance": {"labour": 3, "unknown": 1},
            "required_groups": [["labour"]],
            "expected_suitable": ["labour"],
        },
        {
            "id": "identifier",
            "text": "labour",
            "origin": "fixture",
            "split": "dev",
            "evidence_ids": ["paper"],
            "constraints": {},
            "relevance": {"labour": 3},
            "required_groups": [],
            "expected_suitable": ["labour"],
        },
        {
            "id": "no-answer",
            "text": "Portsmouth unemployment 1900",
            "origin": "fixture",
            "split": "test",
            "evidence_ids": ["paper"],
            "constraints": {"place": "Portsmouth", "period_start": 1900, "period_end": 1900},
            "relevance": {},
            "required_groups": [],
            "expected_suitable": [],
        },
    ]
    _save_json(root / "corpus.json", {"documents": docs})
    _save_json(root / "questions.json", {"questions": queries})
    _save_json(root / "evidence.json", {"sources": [{"id": "paper", "url": "https://example.org"}]})
    np.savez(
        root / "embeddings.npz",
        doc_ids=np.array([doc["id"] for doc in docs]),
        query_ids=np.array([query["id"] for query in queries]),
        document_vectors=np.array([[0, 1], [1, 0], [0.2, 0.8], [0.4, 0.6]], dtype=np.float32),
        query_vectors=np.array([[1, 0], [1, 0], [1, 0]], dtype=np.float32),
    )
    _freeze(root)
    return root


def test_lexical_semantic_and_hybrid_are_distinct_and_deterministic(bundle_dir):
    bundle = load_bundle(bundle_dir)
    lexical = rank(bundle, "paraphrase", "lexical")
    semantic = rank(bundle, "paraphrase", "semantic")
    assert lexical[0]["id"] == "benefits"
    assert semantic[0]["id"] == "labour"
    assert semantic[0]["score"] == 1.0
    assert lexical[0]["status"] == "ineligible"
    assert semantic[0]["status"] == "eligible"
    assert {entry["id"] for entry in rank(bundle, "paraphrase", "hybrid")} == {
        doc["id"] for doc in bundle.documents
    }
    assert rank(bundle, "identifier", "lexical")[0]["id"] == "labour"
    assert run_benchmark(bundle) == run_benchmark(bundle_dir)


def test_coverage_checks_do_not_substitute_claimants_or_wales_for_portsmouth(bundle_dir):
    bundle = load_bundle(bundle_dir)
    constraints = {**bundle.questions[0]["constraints"], "place": "Portsmouth"}
    assert check_coverage(bundle.documents[0], constraints)["status"] == "ineligible"
    assert check_coverage(bundle.documents[1], constraints)["status"] == "eligible"
    assert check_coverage(bundle.documents[2], constraints)["status"] == "ineligible"
    assert check_coverage(bundle.documents[3], constraints)["status"] == "unknown"
    assert (
        "Metadata does not establish requested place coverage"
        in check_coverage(bundle.documents[3], constraints)["reasons"]
    )


def test_country_and_geography_coverage_requires_explicit_granularity():
    doc = document("all", "All local authorities", places=["all_england_local_authorities"])
    assert check_coverage(doc, {"place": "E06000044"})["status"] == "unknown"
    assert (
        check_coverage(doc, {"place": "E06000044"}, policy=LEGACY_COVERAGE_POLICY)["status"]
        == "eligible"
    )
    doc["coverage"]["geography_levels"] = ["country"]
    assert check_coverage(doc, {"place": "Portsmouth"})["status"] == "unknown"
    doc["coverage"]["places"] = []
    assert check_coverage(doc, {"country": "UK"})["status"] == "ineligible"
    doc["coverage"]["countries"] = ["United Kingdom"]
    assert check_coverage(doc, {"country": "England"})["status"] == "eligible"
    assert check_coverage(doc, {"place": "Portsmouth"})["status"] == "unknown"
    doc["coverage"]["countries"] = ["England", "Wales", "Scotland", "Northern Ireland"]
    assert check_coverage(doc, {"country": "UK"})["status"] == "eligible"


def test_generic_place_aliases_and_membership_require_retained_source_evidence():
    doc = document("all", "Local statistics", places=["all_england_local_authorities"])
    constraints = {"place": "E06000023", "geography_vintage": "2021"}
    membership = {
        "place": "Bristol",
        "aliases": ["E06000023"],
        "member_of": ["all_england_local_authorities"],
        "geography_level": "local_authority",
        "geography_vintage": "2021",
        "evidence_ids": ["paper"],
    }
    doc["coverage"]["place_evidence"] = [membership]
    assert check_coverage(doc, constraints)["status"] == "eligible"
    for field, value in (
        ("evidence_ids", ["unbound-source"]),
        ("geography_vintage", "2011"),
        ("geography_level", "LSOA"),
    ):
        changed = copy.deepcopy(doc)
        changed["coverage"]["place_evidence"][0][field] = value
        assert check_coverage(changed, constraints)["status"] == "unknown"
    doc["coverage"]["places"] = ["Bristol"]
    membership.pop("member_of")
    assert check_coverage(doc, constraints)["status"] == "eligible"
    membership["evidence_ids"] = []
    assert check_coverage(doc, constraints)["status"] == "ineligible"


def test_manifest_policy_pins_historical_coverage_and_new_bundles_are_explicit(bundle_dir):
    bundle = load_bundle(bundle_dir)
    original = run_benchmark(bundle)
    assert coverage_policy(bundle) == LEGACY_COVERAGE_POLICY
    assert rank(bundle, bundle.questions[0], "semantic")[0]["status"] == "eligible"
    config = {**bundle.manifest["configuration"], "coverage_policy": DEFAULT_COVERAGE_POLICY}
    _freeze(bundle_dir, configuration=config)
    generic = load_bundle(bundle_dir)
    assert coverage_policy(generic) == DEFAULT_COVERAGE_POLICY
    assert rank(generic, generic.questions[0], "semantic")[0]["status"] == "ineligible"
    _freeze(bundle_dir, configuration=bundle.manifest["configuration"])
    assert run_benchmark(bundle_dir)["replay_sha256"] == original["replay_sha256"]


def test_geography_level_checks_declared_granularity_and_unknowns():
    doc = document("fixture", "Local statistics")
    assert check_coverage(doc, {"geography_level": "local authority"})["status"] == "eligible"
    assert check_coverage(doc, {"geography_level": "LSOA"})["status"] == "ineligible"
    doc["coverage"]["geography_levels"] = []
    assert check_coverage(doc, {"geography_level": "LSOA"})["status"] == "unknown"
    assert (
        check_coverage(doc, {"geography_level": "LSOA", "country": "Wales"})["status"]
        == "ineligible"
    )


def test_release_year_is_distinct_from_observation_time_and_vintage():
    doc = document("release", "Index")
    doc["coverage"].update({"period_start": 2019, "period_end": 2019})
    assert check_coverage(doc, {"release": 2025})["status"] == "eligible"
    assert check_coverage(doc, {"period_start": 2025})["status"] == "ineligible"
    assert check_coverage(doc, {"geography_vintage": 2011})["status"] == "ineligible"
    assert check_coverage(doc, {"period_start": 2018, "period_end": 2019})["status"] == "ineligible"
    doc["coverage"].update({"period_start": None, "period_end": None, "release": None})
    assert check_coverage(doc, {"release": 2025, "period_start": 2025})["status"] == "unknown"
    assert check_coverage(doc, {})["status"] == "eligible"


def test_unknown_placeholders_remain_unknown_instead_of_failing_as_known_mismatches():
    doc = document("missing", "Missing metadata")
    doc["access"] = "unknown"
    doc["concepts"] = ["unspecified"]
    doc["coverage"].update(
        {
            "release": "unknown",
            "countries": ["not specified"],
            "places": ["unknown"],
            "geography_vintages": ["unknown"],
        }
    )
    for constraints in (
        {"access": "open"},
        {"release": "2025"},
        {"country": "England"},
        {"place": "Portsmouth"},
        {"concept": "unemployment"},
        {"geography_vintage": "2021"},
    ):
        assert check_coverage(doc, constraints)["status"] == "unknown"


def test_metrics_exclude_no_answers_from_recall_and_keep_failures_visible(bundle_dir):
    report = run_benchmark(bundle_dir)
    assert report["counts"]["no_relevant_answer_questions"] == 1
    assert report["counts"]["question_origins"] == {"fixture": 3}
    semantic = report["methods"]["semantic"]
    assert semantic["overall"]["recall@5"]["questions"] == 2
    assert semantic["by_split"]["dev"]["recall@5"]["questions"] == 1
    assert semantic["by_origin"]["fixture"]["recall@5"]["questions"] == 2
    rows = {row["question_id"]: row for row in semantic["questions"]}
    assert rows["no-answer"]["metrics"]["recall@5"] is None
    assert rows["no-answer"]["metrics"]["unknown_coverage@5"] == 1
    assert rows["no-answer"]["metrics"]["no_answer_eligible_results@5"] == 0
    assert rows["paraphrase"]["metrics"]["suitable_recall@5"] == 1.0
    assert rows["paraphrase"]["metrics"]["eligible_collection_coverage@5"] == 1.0
    assert rows["paraphrase"]["metrics"]["known_violations@5"] == 2


def test_unjudged_suitability_is_not_counted_as_a_no_answer(bundle_dir):
    path = bundle_dir / "questions.json"
    questions = json.loads(path.read_text())
    for question in questions["questions"]:
        question["suitability_judged"] = False
    _save_json(path, questions)
    _freeze(bundle_dir)
    report = run_benchmark(bundle_dir)
    assert report["counts"]["unjudged_suitability_questions"] == 3
    assert report["counts"]["no_suitable_answer_questions"] == 0
    for method in report["methods"].values():
        assert method["overall"]["recall@5"]["questions"] == 2
        assert method["overall"]["suitable_recall@5"]["questions"] == 0
        for row in method["questions"]:
            assert row["suitability_judged"] is False
            assert row["metrics"]["no_answer_eligible_results@5"] is None
            assert row["metrics"]["first_suitable_reciprocal_rank"] is None
            assert row["metrics"]["eligible_collection_coverage@5"] is None


def test_multisource_full_coverage_requires_each_group(bundle_dir):
    queries_path = bundle_dir / "questions.json"
    payload = json.loads(queries_path.read_text())
    payload["questions"][0].update(
        {
            "constraints": {},
            "relevance": {"labour": 3, "wales": 3},
            "expected_suitable": ["labour", "wales"],
            "required_groups": [["labour"], ["wales"]],
        }
    )
    _save_json(queries_path, payload)
    _freeze(bundle_dir, configuration={"metric_cutoffs": [1, 4]})
    row = next(
        row
        for row in run_benchmark(bundle_dir)["methods"]["semantic"]["questions"]
        if row["question_id"] == "paraphrase"
    )
    assert row["metrics"]["full_collection_coverage@1"] == 0.0
    assert row["metrics"]["full_collection_coverage@4"] == 1.0


def test_frozen_replay_works_after_relocation_with_network_disabled(
    bundle_dir, tmp_path, monkeypatch
):
    original = run_benchmark(bundle_dir)
    copied = tmp_path / "fresh-store" / "benchmark"
    shutil.copytree(bundle_dir, copied)
    shutil.rmtree(bundle_dir)

    def fail_network(*args, **kwargs):
        pytest.fail("Offline benchmark attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    assert run_benchmark(copied) == original


def test_search_input_excludes_judgments_and_source_notes():
    doc = document("sample", "Survey")
    doc["metadata_notes"] = "DO NOT LEAK THIS CURATED NOTE"
    doc["provenance"]["secret"] = "PAPER RELEVANCE ANSWER"
    text = searchable_text(doc)
    assert "Survey" in text and "unemployment" in text and "2021" in text
    assert "LEAK" not in text and "RELEVANCE" not in text


@pytest.mark.parametrize(
    "filename", ["corpus.json", "questions.json", "evidence.json", "embeddings.npz"]
)
def test_mutated_artifacts_fail_before_ranking(bundle_dir, filename):
    with (bundle_dir / filename).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(BenchmarkError, match="SHA256 mismatch"):
        run_benchmark(bundle_dir)


@pytest.mark.parametrize(
    "change,match",
    [
        ("duplicate_doc", "Duplicate documents"),
        ("unknown_qrel", "Unknown relevance"),
        ("unknown_evidence", "Unknown evidence"),
        ("unknown_suitable", "Unknown or irrelevant suitable"),
        ("unsupported_constraint", "Unsupported constraint"),
        ("inverted_period", "inverted period"),
    ],
)
def test_invalid_metadata_references_fail_even_with_correct_hashes(bundle_dir, change, match):
    corpus = json.loads((bundle_dir / "corpus.json").read_text())
    questions = json.loads((bundle_dir / "questions.json").read_text())
    if change == "duplicate_doc":
        corpus["documents"].append(copy.deepcopy(corpus["documents"][0]))
    elif change == "unknown_qrel":
        questions["questions"][0]["relevance"]["missing"] = 3
    elif change == "unknown_evidence":
        corpus["documents"][0]["provenance"]["evidence_ids"] = ["missing"]
    elif change == "unknown_suitable":
        questions["questions"][0]["expected_suitable"] = ["benefits"]
    elif change == "unsupported_constraint":
        questions["questions"][0]["constraints"]["magic"] = "yes"
    elif change == "inverted_period":
        corpus["documents"][0]["coverage"].update({"period_start": 2025, "period_end": 2019})
    _save_json(bundle_dir / "corpus.json", corpus)
    _save_json(bundle_dir / "questions.json", questions)
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError, match=match):
        load_bundle(bundle_dir)


@pytest.mark.parametrize(
    "filename,key", [("corpus.json", "documents"), ("questions.json", "questions")]
)
def test_embedded_citation_cannot_disagree_with_authoritative_evidence(bundle_dir, filename, key):
    records = json.loads((bundle_dir / filename).read_text())
    records[key][0]["references"] = [{"id": "paper", "url": "https://wrong.example.org"}]
    _save_json(bundle_dir / filename, records)
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError, match="Embedded references disagree"):
        load_bundle(bundle_dir)


def test_declared_model_dimension_must_match_vectors(bundle_dir):
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    manifest["model"]["dimensions"] = 384
    _save_json(bundle_dir / "manifest.json", manifest)
    with pytest.raises(BenchmarkError, match="Model dimensions disagree"):
        load_bundle(bundle_dir)


@pytest.mark.parametrize(
    "kind", ["zero", "nan", "dimension", "unknown_id", "duplicate_id", "object"]
)
def test_invalid_embeddings_rejected(bundle_dir, kind):
    with np.load(bundle_dir / "embeddings.npz", allow_pickle=False) as archive:
        payload = {key: archive[key] for key in archive.files}
    if kind == "zero":
        payload["document_vectors"][0] = 0
    elif kind == "nan":
        payload["query_vectors"][0, 0] = np.nan
    elif kind == "dimension":
        payload["query_vectors"] = np.ones((3, 3), dtype=np.float32)
    elif kind == "unknown_id":
        payload["query_ids"][0] = "missing"
    elif kind == "duplicate_id":
        payload["doc_ids"][0] = payload["doc_ids"][1]
    elif kind == "object":
        payload["document_vectors"] = payload["document_vectors"].astype(object)
    np.savez(bundle_dir / "embeddings.npz", **payload)
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError):
        load_bundle(bundle_dir)


@pytest.mark.parametrize("payload", [b"", b"PK\x03\x04broken", b"garbage"])
def test_malformed_embedding_archives_raise_benchmark_errors(bundle_dir, payload):
    (bundle_dir / "embeddings.npz").write_bytes(payload)
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError, match="safe embedding archive"):
        load_bundle(bundle_dir)


def test_npy_file_cannot_masquerade_as_npz(bundle_dir):
    with (bundle_dir / "embeddings.npz").open("wb") as stream:
        np.save(stream, np.ones((4, 2), dtype=np.float32))
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError, match="NPZ archive"):
        load_bundle(bundle_dir)


def test_vector_row_order_and_nonunit_norms_do_not_change_results(bundle_dir):
    original = run_benchmark(bundle_dir)
    with np.load(bundle_dir / "embeddings.npz", allow_pickle=False) as archive:
        payload = {key: archive[key] for key in archive.files}
    permutation = [2, 0, 3, 1]
    payload["doc_ids"] = payload["doc_ids"][permutation]
    payload["document_vectors"] = payload["document_vectors"][permutation] * 2
    np.savez(bundle_dir / "embeddings.npz", **payload)
    _freeze(bundle_dir)
    updated = run_benchmark(bundle_dir)
    assert original["methods"] == updated["methods"]
    assert original["replay_sha256"] != updated["replay_sha256"]


@pytest.mark.parametrize(
    "configuration",
    [
        {"search_text_version": "999"},
        {"lexical_weights": [1, 2]},
        {"rrf_k": 0},
        {"metric_cutoffs": [0]},
        {"coverage_policy": "unsupported"},
        {"coverage_policy": []},
    ],
)
def test_unsupported_scoring_configuration_rejected(bundle_dir, configuration):
    _freeze(bundle_dir, configuration=configuration)
    with pytest.raises(BenchmarkError):
        load_bundle(bundle_dir)


def test_unsafe_filenames_and_stale_embedding_input_hashes_rejected(bundle_dir):
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    manifest["files"]["../outside.json"] = "0" * 64
    _save_json(bundle_dir / "manifest.json", manifest)
    with pytest.raises(BenchmarkError, match="safe bundle basenames"):
        load_bundle(bundle_dir)
    del manifest["files"]["../outside.json"]
    manifest["model"]["input_hashes"]["corpus.json"] = "0" * 64
    _save_json(bundle_dir / "manifest.json", manifest)
    with pytest.raises(BenchmarkError, match="input hashes"):
        load_bundle(bundle_dir)


@pytest.mark.parametrize("filename", ["corpus.json", "questions.json", "evidence.json"])
def test_unknown_payload_schema_versions_are_rejected(bundle_dir, filename):
    payload = json.loads((bundle_dir / filename).read_text())
    payload["schema_version"] = 99
    _save_json(bundle_dir / filename, payload)
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError, match="schema_version"):
        load_bundle(bundle_dir)


def test_question_topic_groups_do_not_leak_between_dev_and_test(bundle_dir):
    payload = json.loads((bundle_dir / "questions.json").read_text())
    payload["questions"][0]["topic_group"] = "same-derived-question-family"
    payload["questions"][1]["topic_group"] = "same-derived-question-family"
    _save_json(bundle_dir / "questions.json", payload)
    _freeze(bundle_dir)
    with pytest.raises(BenchmarkError, match="topic_group spans dev and test"):
        load_bundle(bundle_dir)


def test_manifest_requires_complete_frozen_inputs(bundle_dir):
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    del manifest["files"]["questions.json"]
    _save_json(bundle_dir / "manifest.json", manifest)
    with pytest.raises(BenchmarkError, match="hash all required files"):
        load_bundle(bundle_dir)


def test_scores_tie_by_document_id_not_corpus_row_order(bundle_dir):
    with np.load(bundle_dir / "embeddings.npz", allow_pickle=False) as archive:
        payload = {key: archive[key] for key in archive.files}
    payload["document_vectors"][:] = [1, 0]
    np.savez(bundle_dir / "embeddings.npz", **payload)
    _freeze(bundle_dir)
    ranked = rank(load_bundle(bundle_dir), "paraphrase", "semantic")
    assert [row["id"] for row in ranked] == ["benefits", "labour", "unknown", "wales"]


def test_rank_cannot_use_changed_or_unknown_queries(bundle_dir):
    bundle = load_bundle(bundle_dir)
    question = copy.deepcopy(bundle.questions[0])
    question["text"] = "New query without matching frozen vectors"
    with pytest.raises(BenchmarkError, match="unchanged frozen question"):
        rank(bundle, question, "semantic")
    with pytest.raises(BenchmarkError, match="unchanged frozen question"):
        rank(bundle, "missing", "hybrid")
    with pytest.raises(BenchmarkError, match="Unknown method"):
        rank(bundle, "paraphrase", "magic")
