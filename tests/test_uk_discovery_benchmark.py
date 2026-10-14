from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest

from ssdu.discovery_benchmark import check_coverage, load_bundle, searchable_text
from ssdu.literature_benchmark import DerivationError
from ssdu.uk_discovery_benchmark import build_uk_benchmark


def source(identifier, title, url, kind="provider_metadata"):
    return {"id": identifier, "title": title, "url": url, "type": kind}


@pytest.fixture
def inputs():
    metadata = source(
        "metadata", "Nomis catalogue", "https://www.nomisweb.co.uk/api/v01/dataset/def.sdmx.json"
    )
    historic = source("historic", "Welsh index 2014 release", "https://www.gov.wales/wimd-2014")
    publication = source(
        "paper", "An employment study", "https://doi.org/10.1234/STUDY", "publication_reference"
    )
    base_publication = source(
        "base-paper", "An employment study", "https://doi.org/10.1234/study", "research"
    )
    datasets, descriptors = [], []
    for native, title in (
        ("NM_1_1", "Employment and economic activity"),
        ("NM_2_1", "Other local statistics"),
    ):
        identifier = "nomis:" + native
        datasets.append(
            {
                "id": identifier,
                "provider": "nomis",
                "native_id": native,
                "title": title,
                "description": "Official metadata only",
                "coverage": {},
                "source_ids": ["metadata"],
                "references": [metadata],
            }
        )
        descriptors.append(
            {
                "dataset_id": identifier,
                "provider": "nomis",
                "native_id": native,
                "title": title,
                "source_url": "https://www.nomisweb.co.uk/api/v01/dataset/" + native,
                "columns": [{"label": "GEOGRAPHY", "name": "geography"}],
                "source_metadata": {
                    "resource_path": "nomis/catalogue.json",
                    "location": "/catalogue/" + native,
                },
            }
        )
    base_doc = {
        "id": "old_employment",
        "provider": "nomis",
        "native_id": "NM_1_1",
        "title": "Employment and economic activity",
        "description": "Curated official definitions",
        "variables": ["employment rate"],
        "concepts": ["employment"],
        "coverage": {
            "countries": ["England"],
            "places": ["E06000044"],
            "geography_levels": ["local_authority"],
            "geography_vintages": [],
            "period_start": 2012,
            "period_end": 2024,
            "release": None,
        },
        "access": "open",
        "source_urls": [metadata["url"]],
        "provenance": {"kind": "curated", "evidence_ids": ["metadata"]},
        "references": [metadata],
    }
    base_question = {
        "id": "base-q",
        "text": "Which data measure employment in Portsmouth?",
        "origin": "research_adapted",
        "split": "dev",
        "topic_group": "employment",
        "constraints": {"place": "E06000044"},
        "relevance": {"old_employment": 3},
        "required_groups": [["old_employment"]],
        "expected_suitable": ["old_employment"],
        "evidence_ids": ["base-paper"],
        "references": [base_publication],
        "generation": {"method": "agent_curated", "human_validated": False},
    }
    resolution = {
        "id": "resolution-1",
        "identity_level": "specific_dataset",
        "dataset_id": "nomis:NM_1_1",
        "native_id": "NM_1_1",
        "provider": "nomis",
        "title": "EMPIRICAL SECRET TITLE",
        "release": None,
        "provider_evidence": [{"source_id": "metadata"}],
        "publication_evidence": [{"source_id": "paper", "excerpt": "EMPIRICAL SECRET EXCERPT"}],
        "references": [metadata, publication],
    }
    proposal = {
        "id": "new-q",
        "text": "Which data allow a local employment comparison?",
        "resolution_ids": ["resolution-1"],
        "constraints": {"country": "England"},
        "scenario_basis": "EMPIRICAL SECRET PURPOSE",
        "adaptation_notes": "A purpose adaptation",
        "question_kind": "discovery",
        "references": [publication, metadata],
    }
    record = {
        "relationship_id": "relation-1",
        "paper_id": "openalex:W1",
        "family_id": "uk-family:nomis",
        "use_decision": "supported_use",
        "review_basis": "publication_full_text",
        "resolutions": [resolution],
        "questions": [proposal],
        "references": [publication],
        "independently_human_validated": False,
    }
    return (
        {
            "schema_version": 1,
            "datasets": datasets,
            "catalogue_descriptors": descriptors,
            "sources": [metadata],
        },
        {
            "schema_version": 1,
            "provider_snapshot_id": "provider-snapshot",
            "records": [record],
            "sources": [metadata, historic, publication],
        },
        {"schema_version": 1, "documents": [base_doc]},
        {"schema_version": 1, "questions": [base_question]},
        {"schema_version": 1, "sources": [metadata, base_publication]},
    )


def build(inputs):
    return build_uk_benchmark(*inputs, validation_snapshot_id="validation-snapshot")


def test_canonical_merge_labels_and_official_portsmouth_coverage(inputs):
    result = build(inputs)
    documents = {d["id"]: d for d in result["corpus"]["documents"]}
    questions = {q["id"]: q for q in result["questions"]["questions"]}
    assert set(documents) == {"nomis:NM_1_1", "nomis:NM_2_1"}
    assert result["derivation"]["base_dataset_id_map"] == {"old_employment": "nomis:NM_1_1"}
    assert questions["base-q"]["relevance"] == {"nomis:NM_1_1": 3}
    assert questions["base-q"]["expected_suitable"] == ["nomis:NM_1_1"]
    assert questions["base-q"]["required_groups"] == [["nomis:NM_1_1"]]
    assert check_coverage(documents["nomis:NM_1_1"], {"place": "E06000044"})["status"] == "eligible"
    assert result["derivation"]["coverage_policy"] == "declared-geography-v2"
    legacy = build_uk_benchmark(
        *inputs, validation_snapshot_id="validation-snapshot", coverage_policy="legacy-v1"
    )
    assert "coverage_policy" not in legacy["derivation"]
    assert (
        check_coverage(documents["nomis:NM_1_1"], {"place": "E06000044"}, policy="legacy-v1")[
            "status"
        ]
        == "eligible"
    )
    assert check_coverage(documents["nomis:NM_2_1"], {"place": "E06000044"})["status"] == "unknown"
    assert questions["new-q"]["relevance"] == {"nomis:NM_1_1": 3}
    assert questions["new-q"]["suitability_judged"] is False
    assert questions["new-q"]["expected_suitable"] == []


def test_all_rows_have_resolvable_references_and_pending_human_review(inputs):
    result = build(inputs)
    ids = {s["id"] for s in result["evidence"]["sources"]}
    for row in (
        result["corpus"]["documents"]
        + result["questions"]["questions"]
        + result["review_pack"]["records"]
    ):
        assert row["references"]
        assert {r["id"] for r in row["references"]} <= ids
        assert all(r["title"] and r["url"] for r in row["references"])
    assert all(
        r["status"] == "pending" and r["reviewer"] is None for r in result["review_pack"]["records"]
    )
    assert result["review_pack"]["snapshot_id"] == "validation-snapshot"


def test_publication_text_questions_labels_never_enter_search_metadata(inputs):
    result = build(inputs)
    text = "\n".join(searchable_text(d) for d in result["corpus"]["documents"])
    assert "EMPIRICAL SECRET" not in text
    assert "Which data allow" not in text
    assert "supported_use" not in text
    assert "Other local statistics" in text
    assert result["corpus"]["documents"][0]["provenance"]["unabridged_metadata_sha256"]


def test_deterministic_reordering_does_not_mutate_inputs(inputs):
    original = copy.deepcopy(inputs)
    expected = build(inputs)
    reversed_inputs = copy.deepcopy(inputs)
    for document in reversed_inputs:
        for field in (
            "datasets",
            "catalogue_descriptors",
            "sources",
            "documents",
            "questions",
            "records",
        ):
            if field in document:
                document[field].reverse()
    actual = build(reversed_inputs)
    # Evidence-binding hashes intentionally reflect the frozen input's exact array order.
    for key in ("corpus", "questions", "evidence"):
        assert actual[key] == expected[key]
    assert actual["derivation"]["input_hashes"] == expected["derivation"]["input_hashes"]
    assert inputs == original


def test_duplicate_dois_and_titles_share_split_and_preserve_unambiguous_base_split(inputs):
    record = copy.deepcopy(inputs[1]["records"][0])
    record["relationship_id"] = "relation-2"
    record["paper_id"] = "openalex:W2"
    record["questions"][0]["id"] = "duplicate-q"
    inputs[1]["records"].append(record)
    result = build(inputs)
    questions = result["questions"]["questions"]
    assert {q["split"] for q in questions} == {"dev"}
    assert len({q["topic_group"] for q in questions}) == 1
    assert any("doi:10.1234/study" in q["split_identity_keys"] for q in questions)


def test_cross_split_base_publication_is_regrouped_with_explicit_change_report(inputs):
    question = copy.deepcopy(inputs[3]["questions"][0])
    question.update({"id": "base-q2", "split": "test", "topic_group": "second-topic"})
    inputs[3]["questions"].append(question)
    result = build(inputs)
    assert len({q["split"] for q in result["questions"]["questions"]}) == 1
    assert len(result["derivation"]["split_changes"]) == 1
    assert result["derivation"]["split_changes"][0]["question_id"] in {"base-q", "base-q2"}


def test_family_metadata_does_not_take_paper_title_or_constraint_coverage(inputs):
    record = inputs[1]["records"][0]
    resolution = record["resolutions"][0]
    resolution.update(
        {
            "identity_level": "family",
            "dataset_id": "uk-family:nomis",
            "native_id": None,
            "title": "SECRET PAPER-SPECIFIC POPULATION AND GEOGRAPHY",
            "provider_evidence": [],
        }
    )
    record["questions"][0]["constraints"] = {
        "place": "E06000044",
        "period_start": 2019,
        "period_end": 2019,
    }
    result = build(inputs)
    family = next(d for d in result["corpus"]["documents"] if d["id"] == "uk-family:nomis")
    assert family["title"] == "Nomis census and labour market statistics"
    assert family["coverage"]["places"] == []
    assert family["coverage"]["period_start"] is None
    assert "SECRET" not in searchable_text(family)
    assert check_coverage(family, {"place": "E06000044"})["status"] == "unknown"
    assert next(q for q in result["questions"]["questions"] if q["id"] == "new-q")[
        "identity_levels"
    ] == ["family"]


def test_unresolved_native_identity_uses_controlled_family_without_invented_native_id(inputs):
    resolution = inputs[1]["records"][0]["resolutions"][0]
    resolution.update(
        {"identity_level": "family", "dataset_id": None, "native_id": None, "provider_evidence": []}
    )
    result = build(inputs)
    document = next(d for d in result["corpus"]["documents"] if d["id"] == "uk-family:nomis")
    assert document["native_id"] == ""
    assert document["identity"]["publisher_native_id"] is None
    assert result["derivation"]["resolution_dataset_id_map"]["resolution-1"] == "uk-family:nomis"


def test_historical_release_has_separate_synthetic_identity_not_observation_period(inputs):
    record = inputs[1]["records"][0]
    record["family_id"] = "uk-family:welsh-deprivation"
    resolution = record["resolutions"][0]
    resolution.update(
        {
            "identity_level": "specific_release",
            "dataset_id": "external-wimd-2014",
            "native_id": None,
            "provider": "Welsh Government",
            "title": "Welsh Index of Multiple Deprivation 2014",
            "release": "2014",
            "provider_evidence": [{"source_id": "historic"}],
        }
    )
    record["questions"][0]["constraints"] = {
        "release": "2014",
        "period_start": 2011,
        "period_end": 2011,
    }
    result = build(inputs)
    target = result["derivation"]["resolution_dataset_id_map"]["resolution-1"]
    assert target.startswith("resolved-release:")
    document = next(d for d in result["corpus"]["documents"] if d["id"] == target)
    assert document["coverage"]["release"] == "2014"
    assert document["coverage"]["period_start"] is None
    assert document["identity"]["synthetic"] is True
    assert document["identity"]["data_bytes_pinned"] is False
    assert document["native_id"] == ""
    assert document["coverage"]["countries"] == ["Wales"]
    assert {r["id"] for r in document["references"]} == {"historic"}
    assert check_coverage(document, {"release": "2019"})["status"] == "ineligible"
    assert (
        check_coverage(document, {"country": "England", "place": "Portsmouth"})["status"]
        == "ineligible"
    )
    assert (
        check_coverage(document, {"period_start": 2011, "period_end": 2011})["status"] == "unknown"
    )


def test_named_release_enrichment_requires_official_evidence_not_period_guess(inputs):
    resolution = inputs[1]["records"][0]["resolutions"][0]
    resolution.update({"identity_level": "specific_release", "release": "Census 2011"})
    result = build(inputs)
    document = next(d for d in result["corpus"]["documents"] if d["id"] == "nomis:NM_1_1")
    assert document["coverage"]["release"] == "Census 2011"
    assert (
        document["coverage"]["period_start"] == 2012
    )  # Previously curated metadata remains unchanged.


def test_same_official_welsh_release_reuses_identity_across_titles_and_papers(inputs):
    first = inputs[1]["records"][0]
    first["family_id"] = "uk-family:welsh-deprivation"
    first["resolutions"][0].update(
        {
            "identity_level": "specific_release",
            "dataset_id": None,
            "native_id": None,
            "provider": "Welsh Government",
            "title": "Welsh Index of Multiple Deprivation 2019",
            "release": "2019",
            "provider_evidence": [{"source_id": "historic"}],
        }
    )
    second = copy.deepcopy(first)
    second.update({"relationship_id": "relation-2", "paper_id": "openalex:W2"})
    second["resolutions"][0].update(
        {
            "id": "resolution-2",
            "provider": "welsh_government",
            "title": "Welsh Index of Multiple Deprivation (WIMD) 2019",
        }
    )
    second["questions"][0].update({"id": "new-q2", "resolution_ids": ["resolution-2"]})
    inputs[1]["records"].append(second)
    result = build(inputs)
    mapping = result["derivation"]["resolution_dataset_id_map"]
    assert mapping["resolution-1"] == mapping["resolution-2"]
    target = mapping["resolution-1"]
    document = next(d for d in result["corpus"]["documents"] if d["id"] == target)
    assert document["provider"] == "welsh_government"
    assert document["title"] == "Welsh Index of Multiple Deprivation 2019"
    for question in result["questions"]["questions"]:
        if question["id"].startswith("new-q"):
            assert question["target_coverage"][target]["status"] == "ineligible"
            assert question["expected_suitable"] == []
            assert question["suitability_judged"] is False


def test_nonuse_records_are_audited_without_positive_questions(inputs):
    record = inputs[1]["records"][0]
    record.update({"use_decision": "citation_only", "questions": []})
    result = build(inputs)
    assert result["derivation"]["counts"]["new_questions"] == 0
    assert [q["id"] for q in result["questions"]["questions"]] == ["base-q"]
    assert result["derivation"]["resolution_dataset_id_map"]


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda x: x[1]["records"][0].update(use_decision="citation_only"),
            "supported publication use",
        ),
        (
            lambda x: x[1]["records"][0]["resolutions"][0].update(native_id="NM_999_1"),
            "native identity",
        ),
        (
            lambda x: x[1]["records"][0]["resolutions"][0].update(
                provider_evidence=[{"source_id": "paper"}]
            ),
            "Publication evidence",
        ),
        (
            lambda x: x[1]["records"][0]["questions"][0].update(resolution_ids=["unrelated"]),
            "publication's resolved identities",
        ),
        (lambda x: x[1]["records"][0]["questions"][0].update(references=[]), "known references"),
    ],
)
def test_malformed_or_unproven_binding_fails_closed(inputs, mutate, match):
    mutate(inputs)
    with pytest.raises(DerivationError, match=match):
        build(inputs)


def test_suitability_requires_explicit_fully_satisfied_constraints(inputs):
    proposal = inputs[1]["records"][0]["questions"][0]
    proposal["suitability_judged"] = True
    proposal["constraints"] = {"country": "England", "place": "E06000044"}
    result = build(inputs)
    question = next(q for q in result["questions"]["questions"] if q["id"] == "new-q")
    assert question["expected_suitable"] == ["nomis:NM_1_1"]
    assert question["generation"]["human_validated"] is False
    proposal["constraints"]["place"] = "unknown-place"
    with pytest.raises(DerivationError, match="fully satisfied"):
        build(inputs)


def test_projection_caps_metadata_with_hash_and_truncation_disclosure(inputs):
    dataset = inputs[0]["datasets"][1]
    dataset["title"] = "metadata " * 100
    dataset["description"] = "official description " * 100
    inputs[0]["catalogue_descriptors"][1]["columns"] = [
        {"label": "variable " * 10} for _ in range(8)
    ]
    result = build(inputs)
    document = next(d for d in result["corpus"]["documents"] if d["id"] == "nomis:NM_2_1")
    assert len(document["title"]) == 120 and len(document["description"]) == 240
    assert len(document["variables"]) == 4
    assert all(len(v) == 24 for v in document["variables"])
    assert document["projection"]["truncated_fields"] == ["title", "description", "variables"]
    assert document["provenance"]["resource_pointer"]["resource_path"] == "nomis/catalogue.json"


def test_exact_concepts_are_preserved_for_hard_eligibility(inputs):
    inputs[2]["documents"][0]["concepts"] = [
        "multiple_deprivation",
        "income_deprivation",
        "employment_deprivation",
        "child_income_deprivation",
        "older_people_income_deprivation",
        "population_denominator",
    ]
    result = build(inputs)
    document = next(d for d in result["corpus"]["documents"] if d["id"] == "nomis:NM_1_1")
    assert document["concepts"] == inputs[2]["documents"][0]["concepts"]
    assert "concepts" not in document["projection"]["truncated_fields"]
    for concept in ("older_people_income_deprivation", "population_denominator"):
        assert check_coverage(document, {"concept": concept})["status"] == "eligible"


@pytest.fixture
def frozen_uk_bundle():
    root = Path(__file__).resolve().parents[1] / "benchmarks" / "uk-literature-discovery"
    if not (root / "manifest.json").exists():
        pytest.skip("The separately prepared UK evidence bundle has not yet been frozen")
    return load_bundle(root)


def test_frozen_uk_bundle_complete_pool_references_and_vector_alignment(frozen_uk_bundle):
    bundle = frozen_uk_bundle
    derivation = json.loads((bundle.root / "derivation.json").read_text())
    assert derivation["counts"]["provider_candidates"] == 2472
    assert derivation["counts"]["reviewed_relationships"] == 337
    assert derivation["counts"]["base_questions"] == 48
    assert len(bundle.documents) == derivation["counts"]["documents"]
    assert len(bundle.questions) == derivation["counts"]["questions"]
    assert bundle.document_vectors.shape == (len(bundle.documents), 384)
    assert bundle.query_vectors.shape == (len(bundle.questions), 384)
    source_ids = {s["id"] for s in bundle.evidence}
    for row in bundle.documents + bundle.questions:
        assert row["references"]
        assert {r["id"] for r in row["references"]} <= source_ids
        assert all(r["url"] and r["title"] for r in row["references"])
    document_ids = {d["id"] for d in bundle.documents}
    provider_archive = (
        bundle.root.parents[1] / "examples" / "harvest" / "providers.ssdu-literature.zip"
    )
    with zipfile.ZipFile(provider_archive) as archive:
        provider_ids = {d["id"] for d in json.loads(archive.read("document.json"))["datasets"]}
    assert len(provider_ids) == 2472
    assert provider_ids <= document_ids
    assert set(derivation["base_dataset_id_map"].values()) <= document_ids
    assert set(derivation["resolution_dataset_id_map"].values()) <= document_ids


def test_frozen_uk_bundle_has_no_publication_component_split_overlap(frozen_uk_bundle):
    identity_splits = {}
    for question in frozen_uk_bundle.questions:
        assert question["split_identity_keys"]
        for identity in question["split_identity_keys"]:
            identity_splits.setdefault(identity, set()).add(question["split"])
    assert all(len(splits) == 1 for splits in identity_splits.values())
    # Existing frozen inputs still verify independently; expanded copies carry regrouping.
    original = load_bundle(frozen_uk_bundle.root.parent / "discovery")
    assert len(original.documents) == 33 and len(original.questions) == 48
    assert (
        len({q["id"] for q in frozen_uk_bundle.questions} & {q["id"] for q in original.questions})
        == 48
    )
    derivation = json.loads((frozen_uk_bundle.root / "derivation.json").read_text())
    documents = {d["id"]: d for d in frozen_uk_bundle.documents}
    original_documents = {d["id"]: d for d in original.documents}
    for question in original.questions:
        for identifier in question.get("expected_suitable", []):
            assert check_coverage(
                documents[derivation["base_dataset_id_map"][identifier]], question["constraints"]
            ) == check_coverage(original_documents[identifier], question["constraints"])


def test_frozen_uk_bundle_rejects_welsh_input_for_portsmouth(frozen_uk_bundle):
    bundle = frozen_uk_bundle
    question = next(q for q in bundle.questions if q["id"] == "welsh-a-q36-2")
    documents = {d["id"]: d for d in bundle.documents}
    targets = [documents[i] for i, grade in question["relevance"].items() if grade > 0]
    assert targets and all(d["coverage"]["countries"] == ["Wales"] for d in targets)
    assert all(
        check_coverage(d, question["constraints"])["status"] == "ineligible" for d in targets
    )
    assert question["expected_suitable"] == []
    assert question["suitability_judged"] is False
