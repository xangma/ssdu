"""Traceable silver labels, independent review boundaries, and deterministic files."""

import copy
import hashlib
import json

import numpy as np
import pytest

from ssdu.discovery_benchmark import check_coverage, load_bundle, searchable_text
from ssdu.literature_benchmark import (
    DerivationError,
    derive_benchmark,
    import_review,
    write_inputs,
)
from ssdu.literature_sources import RICH_CONTEXT, VOCABULARY, parse_rich_context
from ssdu.literature_store import LiteratureStore


@pytest.fixture
def evidence():
    sources = [
        {
            "id": "metadata",
            "type": "metadata",
            "title": "Metadata source",
            "url": "https://example.org/metadata",
            "published_at": None,
        },
        {
            "id": "paper-source",
            "type": "research",
            "title": "Research source",
            "url": "https://example.org/paper",
            "published_at": "2020-01-01",
        },
    ]
    datasets = [
        {
            "id": name,
            "title": title,
            "provider": "fixture",
            "native_id": name,
            "description": "Dataset metadata only",
            "coverage": {},
            "source_ids": ["metadata"],
        }
        for name, title in [
            ("used", "Household panel"),
            ("asserted", "Administrative source"),
            ("cited", "Survey index"),
            ("mentioned", "Historical estimates"),
        ]
    ]
    papers = [
        {
            "id": "publication-one",
            "title": "Education and housing inequality",
            "abstract": "SECRET_ABSTRACT_PORTSMOUTH",
            "source_ids": ["paper-source"],
        },
        {
            "id": "publication-two",
            "title": "Follow-up outcomes",
            "abstract": None,
            "source_ids": ["paper-source"],
        },
    ]
    relations = []
    for index, (dataset_id, relation_type) in enumerate(
        [
            ("used", "verified_use"),
            ("asserted", "source_asserted_use"),
            ("cited", "citation"),
            ("mentioned", "inferred_mention"),
        ]
    ):
        relations.append(
            {
                "id": f"relation-{index}",
                "paper_id": "publication-one",
                "dataset_id": dataset_id,
                "relationship_type": relation_type,
                "asserted_by": "fixture provider",
                "evidence": {
                    "source_id": "paper-source",
                    "location": "Methods section",
                    "summary": "SECRET_RELATION_SUMMARY",
                    "url": "https://example.org/paper",
                },
                "review": {"status": "pending", "reviewer": None},
                "source_ids": ["paper-source"],
            }
        )
    relations.append(dict(relations[0], id="relation-other", paper_id="publication-two"))
    return {
        "schema_version": 1,
        "sources": sources,
        "papers": papers,
        "datasets": datasets,
        "relationships": relations,
    }


def test_silver_link_labels_do_not_claim_suitability(evidence):
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot")
    mapping = result["derivation"]["dataset_id_map"]
    question = next(
        q for q in result["questions"]["questions"] if q["paper_id"] == "publication-one"
    )
    assert question["relevance"] == {
        mapping["used"]: 3,
        mapping["asserted"]: 2,
        mapping["cited"]: 1,
        mapping["mentioned"]: 0,
    }
    assert question["expected_suitable"] == []
    assert question["suitability_judged"] is False
    assert question["generation"] == {"method": "title_adapted", "human_validated": False}
    assert question["required_groups"] == [[mapping["asserted"]], [mapping["used"]]]
    assert question["collection_basis"] == "usage_relation_labels"
    assert "lexical title leakage" in " ".join(result["derivation"]["limitations"])


def test_document_search_inputs_exclude_papers_relationships_and_labels(evidence):
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot")
    original = [searchable_text(doc) for doc in result["corpus"]["documents"]]
    changed = copy.deepcopy(evidence)
    changed["papers"][0]["title"] = "REPLACED_PAPER_TITLE"
    changed["papers"][0]["abstract"] = "REPLACED_ABSTRACT"
    changed["relationships"][0]["evidence"]["summary"] = "REPLACED_SUMMARY"
    altered = derive_benchmark(changed, snapshot_id="changed")
    assert original == [searchable_text(doc) for doc in altered["corpus"]["documents"]]
    assert all("SECRET_" not in text and "Education" not in text for text in original)


def test_unknown_geography_and_access_are_preserved(evidence):
    documents = derive_benchmark(evidence, snapshot_id="fixture-snapshot")["corpus"]["documents"]
    for document in documents:
        assert document["coverage"]["countries"] == []
        assert document["coverage"]["places"] == []
        assert document["access"] == "unknown"
        assert check_coverage(document, {"place": "Portsmouth"})["status"] == "unknown"


def test_references_survive_each_record_and_review_template(evidence):
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot")
    for document in result["corpus"]["documents"]:
        assert document["references"][0]["title"] == "Metadata source"
        assert document["references"][0]["url"] == "https://example.org/metadata"
    for question in result["questions"]["questions"]:
        assert question["references"][0]["title"] == "Research source"
    for row in result["review_pack"]["records"]:
        assert row["references"]
        assert row["paper"]["abstract"] in {None, "SECRET_ABSTRACT_PORTSMOUTH"}
        assert row["relationships"][0]["evidence"]["location"] == "Methods section"
        assert row["status"] == "pending" and row["reviewer"] is None


def test_curated_questions_are_grouped_by_paper_and_labels_remain_silver(evidence):
    curation = {
        "questions": [
            {
                "paper_id": "publication-one",
                "text": "Question A",
                "constraints": {"place": "Portsmouth"},
            },
            {"paper_id": "publication-one", "text": "Question B", "relevance": {"cited": 2}},
        ]
    }
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot", curation=curation)
    questions = result["questions"]["questions"]
    assert len(questions) == 3
    selected = [q for q in questions if q["paper_id"] == "publication-one"]
    assert len({q["split"] for q in selected}) == len({q["topic_group"] for q in selected}) == 1
    assert all(q["generation"]["method"] == "agent_curated" for q in selected)
    assert all(q["generation"]["human_validated"] is False for q in selected)
    assert result == derive_benchmark(evidence, snapshot_id="fixture-snapshot", curation=curation)


def test_named_human_review_is_explicit_bound_and_importable(evidence):
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot")
    template = copy.deepcopy(result["review_pack"])
    row = template["records"][0]
    row.update(
        {
            "status": "human_reviewed",
            "reviewer": "Researcher A",
            "text": "A reviewed question",
            "suitability_judged": True,
            "expected_suitable": ["used"],
        }
    )
    curation = import_review(None, template)
    reviewed = derive_benchmark(evidence, snapshot_id="fixture-snapshot", curation=curation)
    question = next(q for q in reviewed["questions"]["questions"] if q["id"] == row["question_id"])
    assert question["text"] == "A reviewed question"
    assert question["generation"]["human_validated"] is True
    assert question["suitability_judged"] is True
    assert question["review"]["reviewer"] == "Researcher A"
    assert len(curation["questions"]) == 1
    with pytest.raises(DerivationError, match="different snapshot"):
        derive_benchmark(evidence, snapshot_id="other", curation=curation)
    changed = copy.deepcopy(evidence)
    changed["papers"][0]["title"] = "Changed title"
    with pytest.raises(DerivationError, match="different source evidence"):
        derive_benchmark(changed, snapshot_id="fixture-snapshot", curation=curation)


def test_review_template_edits_cannot_implicitly_claim_human_validation(evidence):
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot")
    template = result["review_pack"]
    template["records"][0]["text"] = "An edit without completed review"
    curation = import_review(None, template)
    assert curation["questions"] == []
    assert (
        derive_benchmark(evidence, snapshot_id="fixture-snapshot", curation=curation)["questions"]
        == result["questions"]
    )
    template["records"][0]["status"] = "human_reviewed"
    with pytest.raises(DerivationError, match="Human reviewer"):
        import_review(None, template)


@pytest.mark.parametrize(
    "question",
    [
        {"paper_id": "missing", "text": "Question"},
        {"paper_id": "publication-one", "text": ""},
        {"paper_id": "publication-one", "text": "Question", "constraints": {"unsupported": "x"}},
        {"paper_id": "publication-one", "text": "Question", "relevance": {"used": True}},
        {"paper_id": "publication-one", "text": "Question", "relevance": {"missing": 3}},
        {"paper_id": "publication-one", "text": "Question", "expected_suitable": ["mentioned"]},
        {"paper_id": "publication-one", "text": "Question", "required_groups": [[]]},
        {"paper_id": "publication-one", "text": "Question", "suitability_judged": True},
        {
            "paper_id": "publication-one",
            "text": "Question",
            "review": {"status": "human_reviewed", "reviewer": None},
        },
    ],
)
def test_malformed_curation_is_rejected(evidence, question):
    with pytest.raises(DerivationError):
        derive_benchmark(
            evidence, snapshot_id="fixture-snapshot", curation={"questions": [question]}
        )


def test_missing_relationship_targets_and_unreferenced_rows_are_rejected(evidence):
    changed = copy.deepcopy(evidence)
    changed["relationships"][0]["dataset_id"] = "missing"
    with pytest.raises(DerivationError, match="missing paper or dataset"):
        derive_benchmark(changed, snapshot_id="fixture-snapshot")
    changed = copy.deepcopy(evidence)
    changed["datasets"][0]["source_ids"] = []
    with pytest.raises(DerivationError, match="retain a source reference"):
        derive_benchmark(changed, snapshot_id="fixture-snapshot")
    changed = copy.deepcopy(evidence)
    changed["relationships"] = []
    with pytest.raises(DerivationError, match="at least one linked paper"):
        derive_benchmark(changed, snapshot_id="fixture-snapshot")


def test_inputs_are_deterministic_non_overwriting_and_benchmark_compatible(evidence, tmp_path):
    result = derive_benchmark(evidence, snapshot_id="fixture-snapshot")
    one = write_inputs(tmp_path / "one", result)
    two = tmp_path / "two"
    two.mkdir()
    write_inputs(two, result)
    assert {p.name: p.read_bytes() for p in one.iterdir()} == {
        p.name: p.read_bytes() for p in two.iterdir()
    }
    for name, expected in result["derivation"]["input_hashes"].items():
        assert hashlib.sha256((one / name).read_bytes()).hexdigest() == expected
    with pytest.raises(DerivationError, match="absent or empty"):
        write_inputs(one, result)
    link = tmp_path / "symlink"
    link.symlink_to(one, target_is_directory=True)
    with pytest.raises(DerivationError, match="symlink"):
        write_inputs(link, result)
    documents, questions = result["corpus"]["documents"], result["questions"]["questions"]
    np.savez(
        one / "embeddings.npz",
        doc_ids=np.array([d["id"] for d in documents]),
        query_ids=np.array([q["id"] for q in questions]),
        document_vectors=np.ones((len(documents), 2), dtype=np.float32),
        query_vectors=np.ones((len(questions), 2), dtype=np.float32),
    )
    files = {
        name: hashlib.sha256((one / name).read_bytes()).hexdigest()
        for name in ("corpus.json", "questions.json", "evidence.json", "embeddings.npz")
    }
    (one / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": files}))
    bundle = load_bundle(one)
    assert bundle.questions == questions and bundle.documents == documents


@pytest.mark.parametrize("field,value", [("native_id", 4), ("description", {}), ("access", False)])
def test_dataset_metadata_does_not_silently_coerce_malformed_values(evidence, field, value):
    evidence["datasets"][0][field] = value
    with pytest.raises(DerivationError):
        derive_benchmark(evidence, snapshot_id="fixture-snapshot")


def test_review_import_refuses_mixed_evidence_and_malformed_curation(evidence):
    template = derive_benchmark(evidence, snapshot_id="fixture-snapshot")["review_pack"]
    with pytest.raises(DerivationError, match="different source evidence"):
        import_review({"snapshot_id": "other", "questions": []}, template)
    with pytest.raises(DerivationError, match="curation schema"):
        import_review([], template)


def test_parser_store_and_derivation_share_canonical_relationship_contract(tmp_path):
    """Reproduce the integration boundary, including snapshot-first provenance."""
    raw = json.dumps(
        {
            "@context": RICH_CONTEXT,
            "@graph": [
                {
                    "@id": VOCABULARY + "provider-1",
                    "@type": "Provider",
                    "dct:title": {"@value": "Source provider"},
                },
                {
                    "@id": VOCABULARY + "dataset-1",
                    "@type": "Dataset",
                    "dct:title": {"@value": "A source catalogue family"},
                    "dct:publisher": {"@value": "provider-1"},
                },
                {
                    "@id": VOCABULARY + "publication-1",
                    "@type": "ResearchPublication",
                    "dct:title": {"@value": "A linked publication"},
                    "cito:citesAsDataSource": {"@id": VOCABULARY + "dataset-1"},
                },
            ],
        }
    ).encode()
    document = parse_rich_context(
        raw,
        source_url="https://example.org/corpus.jsonld",
        revision="fixed-sha",
        publication_ids=["publication-1"],
    )
    store = LiteratureStore(tmp_path / "source-store")
    snapshot_id = store.publish(document, {"rich-context.jsonld": raw})
    normalized = store.read(snapshot_id)
    assert normalized["relationships"][0]["relationship_type"] == "source_asserted_use"
    assert "type" not in normalized["relationships"][0]
    derived = derive_benchmark(normalized, snapshot_id=snapshot_id)
    mapping = derived["derivation"]["dataset_id_map"]
    question = derived["questions"]["questions"][0]
    assert question["relevance"] == {mapping["rich-context:dataset-1"]: 2}
    assert question["required_groups"] == [[mapping["rich-context:dataset-1"]]]
    assert question["suitability_judged"] is False
    assert question["generation"]["human_validated"] is False
    assert derived["derivation"]["snapshot_id"] == snapshot_id
    assert (
        derived["review_pack"]["records"][0]["relationships"][0] == normalized["relationships"][0]
    )
    write_inputs(tmp_path / "inputs", derived)
