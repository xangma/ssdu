import copy
import hashlib
import json

import pytest

from ssdu.literature_validation import LinkValidationError, validate_link_reviews

SID = "a" * 64


def fixture():
    source = {
        "id": "article",
        "title": "Publication",
        "url": "https://example.test/article",
        "type": "bibliographic_metadata",
        "resource_paths": ["article.json"],
    }
    provider = {
        "id": "official",
        "title": "Official catalogue",
        "url": "https://example.test/catalogue",
        "type": "provider_metadata",
        "resource_paths": ["catalogue.json"],
    }
    reference = lambda row: {k: row[k] for k in ("id", "title", "url", "type")}
    paper = {
        "id": "paper",
        "title": "Deprivation and health",
        "url": source["url"],
        "doi": None,
        "abstract": "We used the deprivation 2019 release for the analysis.",
        "published_at": "2021-01-01",
        "source_ids": ["article"],
        "references": [reference(source)],
    }
    family = {
        "id": "family",
        "title": "Deprivation",
        "provider": "deprivation",
        "description": "",
        "doi": None,
        "version": None,
        "native_id": None,
        "coverage": {},
        "source_ids": ["article"],
        "references": [reference(source)],
    }
    dataset = {
        "id": "deprivation:d2019",
        "title": "Deprivation 2019",
        "provider": "deprivation",
        "description": "Official 2019 release",
        "doi": None,
        "version": None,
        "native_id": "d2019",
        "coverage": {},
        "source_ids": ["official"],
        "references": [reference(provider)],
    }
    relationship = {
        "id": "relation",
        "paper_id": "paper",
        "dataset_id": "family",
        "relationship_type": "inferred_mention",
        "asserted_by": "parser",
        "dataset_version": None,
        "evidence": {
            "source_id": "article",
            "location": "/abstract",
            "summary": "Literal mention",
            "url": source["url"],
        },
        "review": {"status": "pending", "reviewer": None},
        "references": [reference(source)],
    }
    document = {
        "schema_version": 1,
        "sources": [source],
        "papers": [paper],
        "datasets": [family],
        "relationships": [relationship],
    }
    provider_doc = {
        "schema_version": 1,
        "sources": [provider],
        "papers": [],
        "datasets": [dataset],
        "relationships": [],
    }
    evidence = {
        "source_id": "article",
        "field": "abstract",
        "location": "/abstract",
        "excerpt": "deprivation 2019 release",
        "url": source["url"],
    }
    resolution = {
        "id": "resolved",
        "identity_level": "specific_release",
        "dataset_id": dataset["id"],
        "native_id": "d2019",
        "provider": "deprivation",
        "title": "Deprivation 2019",
        "release": "2019",
        "publication_evidence": [copy.deepcopy(evidence)],
        "provider_evidence": [
            {
                "source_id": "official",
                "location": "/datasets/" + dataset["id"],
                "url": provider["url"],
            }
        ],
        "catalogue_candidates": [],
        "source_urls": [source["url"], provider["url"]],
        "rationale": "Explicit release in methods and official catalogue.",
    }
    question = {
        "id": "question",
        "text": "Which deprivation data underpinned health analysis?",
        "resolution_ids": ["resolved"],
        "constraints": {"release": "2019"},
        "scenario_basis": "Published health analysis",
        "adaptation_notes": "Purpose paraphrase",
        "question_kind": "discovery",
    }
    record = {
        "relationship_id": "relation",
        "paper_id": "paper",
        "use_decision": "supported_use",
        "review_basis": "publication_abstract",
        "rationale": "Methods state analytical use.",
        "evidence": [evidence],
        "resolutions": [resolution],
        "questions": [question],
        "references": [reference(source)],
        "independently_human_validated": False,
    }
    review = {
        "schema_version": 1,
        "source_snapshot_id": SID,
        "family_id": "family",
        "records": [record],
        "sources": [],
        "resources": [],
    }
    resources = {
        "article.json": json.dumps(paper).encode(),
        "catalogue.json": json.dumps(dataset).encode(),
    }
    return document, provider_doc, [review], resources


def validate(values=None):
    doc, provider, reviews, resources = values or fixture()
    return validate_link_reviews(
        doc, provider, reviews, source_snapshot_id=SID, resources=resources
    )


def test_complete_grounded_report_has_stable_counts_and_references_without_mutating_input():
    values = fixture()
    before = copy.deepcopy(values)
    report = validate(values)
    assert values == before
    assert report["counts"] == {
        "relationships": 1,
        "papers": 1,
        "use_decisions": {"supported_use": 1},
        "identity_levels": {"specific_release": 1},
        "unique_resolutions": 1,
        "questions": 1,
    }
    assert report == validate(values)
    span = report["records"][0]["evidence"][0]["verified_location"]["verified_spans"][0]
    assert (
        values[0]["papers"][0]["abstract"][span["start"] : span["end"]]
        == "deprivation 2019 release"
    )
    assert report["questions"][0]["paper_id"] == "paper"
    assert {ref["id"] for ref in report["questions"][0]["references"]} == {"article", "official"}
    assert report["resolutions"][0]["benchmark_eligible"]
    assert report["independently_human_validated"] is False


@pytest.mark.parametrize(
    "case,message",
    [
        ("missing", "cover every"),
        ("duplicate", "Duplicate reviewed"),
        ("paper", "paper disagrees"),
        ("family", "family is unknown"),
        ("relationship", "relationship is unknown"),
        ("snapshot", "snapshot mismatch"),
        ("unknown-source", "unknown source"),
        ("excerpt", "literal substring"),
        ("url", "URL disagrees"),
        ("references", "original paper references"),
        ("reference-title", "reference title"),
        ("human", "human validation"),
        ("release", "not literal"),
        ("native", "native ID"),
        ("dataset", "absent from provider"),
        ("constraints", "Unsupported question"),
        ("empty-resolution", "local resolutions"),
        ("non-use-question", "supported use"),
        ("question-id", "Duplicate global"),
        ("fulltext-basis", "lacks captured"),
        ("missing-resource", "Missing source raw"),
    ],
)
def test_reject_incomplete_or_fabricated_ledger(case, message):
    values = fixture()
    _doc, provider, reviews, resources = values
    review, row = reviews[0], reviews[0]["records"][0]
    if case == "missing":
        review["records"] = []
    elif case == "duplicate":
        review["records"].append(copy.deepcopy(row))
    elif case == "paper":
        row["paper_id"] = "wrong"
    elif case == "family":
        review["family_id"] = "wrong"
    elif case == "relationship":
        row["relationship_id"] = "wrong"
    elif case == "snapshot":
        review["source_snapshot_id"] = "b" * 64
    elif case == "unknown-source":
        row["evidence"][0]["source_id"] = "wrong"
    elif case == "excerpt":
        row["evidence"][0]["excerpt"] = "We used the deprivation 2025 release"
    elif case == "url":
        row["evidence"][0]["url"] = "https://example.test/fabricated"
    elif case == "references":
        row["references"] = [copy.deepcopy(provider["datasets"][0]["references"][0])]
    elif case == "reference-title":
        row["references"][0]["title"] = "invented"
    elif case == "human":
        row["independently_human_validated"] = True
    elif case == "release":
        row["resolutions"][0]["release"] = "2025"
    elif case == "native":
        row["resolutions"][0]["native_id"] = "wrong"
    elif case == "dataset":
        row["resolutions"][0]["dataset_id"] = "unknown"
    elif case == "constraints":
        row["questions"][0]["constraints"]["invented_field"] = "LSOA"
    elif case == "empty-resolution":
        row["questions"][0]["resolution_ids"] = []
    elif case == "non-use-question":
        row["use_decision"] = "citation_only"
    elif case == "question-id":
        row["questions"].append(copy.deepcopy(row["questions"][0]))
    elif case == "fulltext-basis":
        row["review_basis"] = "publication_full_text"
    elif case == "missing-resource":
        del resources["article.json"]
    with pytest.raises(LinkValidationError, match=message):
        validate(values)


def captured_fixture():
    values = fixture()
    _, _, reviews, resources = values
    raw = b"Methods: We used the deprivation 2019 release."
    source = {
        "id": "fulltext",
        "title": "Captured publication text",
        "url": "https://example.test/fulltext",
        "type": "publication_full_text",
        "resource_paths": ["review/fulltext.txt"],
        "text_resource": "review/fulltext.txt",
    }
    reviews[0]["sources"] = [source]
    reviews[0]["resources"] = [
        {"path": "review/fulltext.txt", "sha256": hashlib.sha256(raw).hexdigest()}
    ]
    resources["review/fulltext.txt"] = raw
    row = reviews[0]["records"][0]
    row["review_basis"] = "publication_full_text"
    evidence = {
        "source_id": "fulltext",
        "field": "captured_text",
        "location": {"resource_path": "review/fulltext.txt", "start": 9000, "end": 9999},
        "excerpt": "deprivation 2019 release",
        "url": source["url"],
    }
    row["evidence"] = [evidence]
    row["resolutions"][0]["publication_evidence"] = [copy.deepcopy(evidence)]
    return values


def test_captured_text_checks_bytes_and_computes_spans_independently():
    report = validate(captured_fixture())
    evidence = report["records"][0]["evidence"][0]
    assert evidence["location"]["start"] == 9000
    assert evidence["verified_location"]["verified_spans"][0]["start"] == 21
    assert "fulltext" in {ref["id"] for ref in report["records"][0]["references"]}


def test_corrupt_capture_hash_fails_before_grounding():
    values = captured_fixture()
    values[3]["review/fulltext.txt"] = b"Changed"
    with pytest.raises(LinkValidationError, match="hash mismatch"):
        validate(values)


def test_publication_pointer_can_only_reuse_exact_literal_record_evidence():
    values = fixture()
    pub = values[2][0]["records"][0]["resolutions"][0]["publication_evidence"][0]
    del pub["excerpt"]
    assert (
        validate(values)["resolutions"][0]["publication_evidence"][0]["excerpt"]
        == "deprivation 2019 release"
    )
    pub["location"] = "/wrong"
    with pytest.raises(LinkValidationError, match="literal excerpt"):
        validate(values)


def test_provider_captured_json_pointer_checks_declared_resource():
    values = fixture()
    _, _provider, reviews, resources = values
    resources["catalogue.json"] = json.dumps(
        {"data": [{"id": "d2019", "title": "Deprivation 2019"}]}
    ).encode()
    resolution = reviews[0]["records"][0]["resolutions"][0]
    resolution["provider_evidence"][0].update(
        field="captured_json",
        location={"resource_path": "catalogue.json", "json_pointer": "/data/0"},
    )
    assert validate(values)["counts"]["unique_resolutions"] == 1
    resolution["provider_evidence"][0]["location"]["json_pointer"] = "/data/1"
    with pytest.raises(LinkValidationError, match="does not resolve"):
        validate(values)


def test_non_use_diagnostic_identity_never_becomes_a_positive_question():
    values = fixture()
    row = values[2][0]["records"][0]
    row["use_decision"] = "mention_only"
    row["questions"] = []
    report = validate(values)
    assert not report["resolutions"][0]["benchmark_eligible"]
    assert not report["questions"]


def test_shared_identity_is_deduplicated_without_dropping_paper_context():
    values = fixture()
    doc, _, reviews, _ = values
    paper = copy.deepcopy(doc["papers"][0])
    paper["id"] = "paper2"
    doc["papers"].append(paper)
    relation = copy.deepcopy(doc["relationships"][0])
    relation.update(id="relation2", paper_id="paper2")
    doc["relationships"].append(relation)
    row = copy.deepcopy(reviews[0]["records"][0])
    row.update(relationship_id="relation2", paper_id="paper2")
    row["resolutions"][0]["id"] = "resolved2"
    row["resolutions"][0]["rationale"] = "Different paper interpretation preserved locally."
    row["questions"] = []
    reviews[0]["records"].append(row)
    report = validate(values)
    assert report["counts"]["unique_resolutions"] == 1
    assert report["resolutions"][0]["resolution_ids"] == ["resolved", "resolved2"]
    assert report["resolutions"][0]["paper_ids"] == ["paper", "paper2"]
    assert report["records"][1]["resolutions"][0]["rationale"].startswith("Different")
    reviews[0]["records"].reverse()
    assert validate(values) == report


def test_alternative_openalex_page_requires_exact_cached_paper_and_field():
    values = fixture()
    doc, _, reviews, resources = values
    paper = doc["papers"][0]
    paper.update(
        id="openalex:W1",
        openalex_ids=["https://openalex.org/W1"],
        field_provenance={"abstract": {"source_id": "canonical"}},
    )
    doc["relationships"][0]["paper_id"] = paper["id"]
    reviews[0]["records"][0]["paper_id"] = paper["id"]
    index = {word: [] for word in paper["abstract"].split()}
    for position, word in enumerate(paper["abstract"].split()):
        index[word].append(position)
    raw = {"results": [{"id": "https://openalex.org/W1", "abstract_inverted_index": index}]}
    resources["article.json"] = json.dumps(raw).encode()
    report = validate(values)
    assert report["records"][0]["evidence"][0]["verified_location"]["json_pointer"] == "/results/0"
    raw["results"][0]["id"] = "https://openalex.org/W2"
    resources["article.json"] = json.dumps(raw).encode()
    with pytest.raises(LinkValidationError, match="captured source"):
        validate(values)


def test_provider_pointer_cannot_resolve_another_native_dataset():
    values = fixture()
    resources = values[3]
    resources["catalogue.json"] = json.dumps(
        {"data": [{"id": "d2025", "title": "2019 methodology"}]}
    ).encode()
    proof = values[2][0]["records"][0]["resolutions"][0]["provider_evidence"][0]
    proof.update(
        field="captured_json",
        location={"resource_path": "catalogue.json", "json_pointer": "/data/0"},
    )
    with pytest.raises(LinkValidationError, match="resolved native dataset"):
        validate(values)


def test_historical_release_requires_captured_official_evidence_and_no_fake_native_id():
    values = captured_fixture()
    review = values[2][0]
    raw = b"Official deprivation release 2019 documentation."
    source = {
        "id": "historical",
        "title": "Official historical metadata",
        "url": "https://example.test/history",
        "type": "provider_metadata",
        "resource_paths": ["history.txt"],
        "text_resource": "history.txt",
    }
    review["sources"].append(source)
    review["resources"].append({"path": "history.txt", "sha256": hashlib.sha256(raw).hexdigest()})
    values[3]["history.txt"] = raw
    resolution = review["records"][0]["resolutions"][0]
    resolution.update(
        dataset_id=None,
        native_id=None,
        provider_evidence=[
            {
                "source_id": "historical",
                "field": "captured_text",
                "location": "history.txt",
                "excerpt": "release 2019",
                "url": source["url"],
            }
        ],
        source_urls=[source["url"]],
    )
    assert validate(values)["resolutions"][0]["dataset_id"] is None
    resolution["native_id"] = "unverified-historical-id"
    with pytest.raises(LinkValidationError, match="known catalogue identity"):
        validate(values)


def test_constraints_reject_boolean_periods_and_inverted_ranges():
    values = fixture()
    constraints = values[2][0]["records"][0]["questions"][0]["constraints"]
    constraints.update(period_start=True)
    with pytest.raises(LinkValidationError, match="must be an integer"):
        validate(values)
    constraints.update(period_start=2020, period_end=2019)
    with pytest.raises(LinkValidationError, match="Inverted"):
        validate(values)


def test_candidate_rows_are_referenced_and_cannot_become_positive_labels():
    values = fixture()
    resolution = values[2][0]["records"][0]["resolutions"][0]
    resolution["catalogue_candidates"] = [{"dataset_id": "deprivation:d2019", "native_id": "d2019"}]
    report = validate(values)
    candidate = report["resolutions"][0]["catalogue_candidates"][0]
    assert not candidate["benchmark_eligible"]
    assert candidate["references"][0]["id"] == "official"
    resolution["catalogue_candidates"][0]["native_id"] = "another-native-id"
    with pytest.raises(LinkValidationError, match="candidate native ID"):
        validate(values)


def test_uncatalogued_candidates_require_known_references_and_explicit_diagnostic_status():
    values = fixture()
    resolution = values[2][0]["records"][0]["resolutions"][0]
    candidate = {
        "title": "Possible historic attachment",
        "dataset_id": None,
        "references": copy.deepcopy(values[1]["datasets"][0]["references"]),
        "benchmark_eligible": False,
        "in_frozen_provider_catalogue": False,
    }
    resolution["uncatalogued_candidates"] = [candidate]
    assert validate(values)["counts"]["questions"] == 1
    candidate["benchmark_eligible"] = True
    with pytest.raises(LinkValidationError, match="without a positive label"):
        validate(values)


def test_captured_repository_abstract_does_not_claim_full_text_review():
    values = captured_fixture()
    review = values[2][0]
    review["records"][0]["review_basis"] = "publication_abstract"
    review["sources"][0]["type"] = "publication_repository_metadata"
    assert validate(values)["records"][0]["review_basis"] == "publication_abstract"
    review["sources"][0]["type"] = "publication_full_text"
    with pytest.raises(LinkValidationError, match="Abstract review contains full-text"):
        validate(values)
