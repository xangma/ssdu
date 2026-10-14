"""Full imports account for every source entity without losing failed-row evidence."""

import copy
import json
import socket
import zipfile
from pathlib import Path

import pytest

import ssdu.literature_sources as sources
from ssdu.literature_sources import (
    RICH_CONTEXT,
    VOCABULARY,
    LiteratureSourceError,
    parse_rich_context,
    parse_rich_context_full,
)
from ssdu.literature_store import MAX_DOCUMENT_BYTES, LiteratureStore, validate_document

SOURCE_URL = "https://example.org/pinned/corpus.jsonld"


def literal(value):
    return {"@value": value}


def publication(identifier, dataset="dataset-1"):
    return {
        "@id": VOCABULARY + identifier,
        "@type": "ResearchPublication",
        "dct:title": literal("A social science paper " + identifier),
        "cito:citesAsDataSource": {"@id": VOCABULARY + dataset},
    }


def graph():
    return {
        "@context": RICH_CONTEXT,
        "@graph": [
            {"@id": VOCABULARY + "provider-1", "@type": "Provider", "dct:title": literal("Agency")},
            {
                "@id": VOCABULARY + "dataset-1",
                "@type": "Dataset",
                "dct:title": literal("Household programme"),
                "dct:publisher": literal("provider-1"),
            },
            publication("publication-a"),
            publication("publication-b"),
            {
                "@id": VOCABULARY + "dataset-unused",
                "@type": "Dataset",
                "dct:title": literal("Standalone dataset"),
                "dct:publisher": literal("provider-2"),
            },
            {
                "@id": VOCABULARY + "provider-2",
                "@type": "Provider",
                "dct:title": literal("Agency 2"),
            },
            {
                "@id": VOCABULARY + "provider-3",
                "@type": "Provider",
                "dct:title": literal("Orphan agency"),
            },
        ],
    }


def parse(value, **kwargs):
    return parse_rich_context_full(
        json.dumps(value).encode(), source_url=SOURCE_URL, revision="fixed-sha", **kwargs
    )


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Pure Rich Context full parser attempted networking")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


def assert_valid(document):
    value = validate_document(document, resource_names=["rich-context.jsonld"])
    source_ids = {row["id"] for row in value["sources"]}
    for key in (
        "papers",
        "datasets",
        "providers",
        "relationships",
        "quarantine",
        "unused_dataset_nodes",
    ):
        for row in value[key]:
            assert row["references"]
            assert all(
                ref["id"] in source_ids and ref["title"] and ref["url"] for ref in row["references"]
            )
    report = value["coverage_report"]
    for source, imported, rejected in (
        ("source_publications", "imported_publications", "rejected_publications"),
        ("source_dataset_nodes", "imported_dataset_records", "rejected_dataset_records"),
        ("source_provider_nodes", "imported_provider_records", "rejected_provider_records"),
    ):
        assert report[source] == report[imported] + report[rejected]
    return value


def test_full_graph_retains_standalone_entities_and_explicit_unknowns():
    document = assert_valid(parse(graph()))
    assert len(document["papers"]) == 2
    assert len(document["datasets"]) == 2
    assert len(document["providers"]) == 3
    assert len(document["relationships"]) == 2
    assert document["coverage_report"]["unused_dataset_nodes"] == 1
    assert document["coverage_report"]["providers_unreferenced_by_imported_datasets"] == 1
    assert document["coverage_report"]["uk_relevance"] == "unassessed"
    assert document["quarantine"] == []
    assert document["unused_dataset_nodes"][0]["source_record_id"] == VOCABULARY + "dataset-unused"
    assert document["unused_dataset_nodes"][0]["location"] == "/@graph/4"
    for paper in document["papers"]:
        assert paper["published_at"] is None and paper["doi"] is None and paper["abstract"] is None
        assert paper["source_locations"][0]["source_id"] == "rich-context:fixed-sha"
    for dataset in document["datasets"]:
        assert dataset["version"] is None and dataset["coverage"] == {}
    for relationship in document["relationships"]:
        assert relationship["relationship_type"] == "source_asserted_use"
        assert relationship["review"]["independently_human_validated"] is False


def test_full_import_exceeds_pilot_limit_and_decodes_graph_once(monkeypatch):
    value = graph()
    value["@graph"] = value["@graph"][:2] + [
        publication(f"publication-{i:04}") for i in range(1604)
    ]
    calls = []
    original = sources._raw_json

    def counted(raw):
        calls.append(len(raw))
        return original(raw)

    monkeypatch.setattr(sources, "_raw_json", counted)
    document = assert_valid(parse(value))
    assert len(calls) == 1
    assert len(document["papers"]) == 1604
    assert len(document["relationships"]) == 1604
    assert len(document["datasets"]) == 1


@pytest.mark.parametrize(
    "mutation, reason",
    [
        (lambda node: node.update({"dct:title": "plain string"}), "must be a literal"),
        (lambda node: node.update({"foaf:page": literal("https://example.org/bad url")}), "URL"),
        (lambda node: node.update({"@id": None}), "publication identifier"),
        (
            lambda node: node.update({"@id": "https://example.org/not-a-publication"}),
            "publication identifier",
        ),
        (
            lambda node: node.update(
                {"cito:citesAsDataSource": {"@id": VOCABULARY + "dataset-missing"}}
            ),
            "Broken referenced dataset",
        ),
        (
            lambda node: node.update(
                {"cito:citesAsDataSource": [{"@id": VOCABULARY + "dataset-1"}] * 2}
            ),
            "Duplicate dataset link",
        ),
        (
            lambda node: node.update(
                {"cito:citesAsDataSource": [{"@id": VOCABULARY + "dataset-1"}, {"@id": []}]}
            ),
            "dataset identifier",
        ),
    ],
)
def test_publication_failure_isolated_with_exact_raw_pointer(mutation, reason):
    value = graph()
    mutation(value["@graph"][3])
    document = assert_valid(parse(value))
    assert [row["id"] for row in document["papers"]] == ["rich-context:publication-a"]
    assert [row["paper_id"] for row in document["relationships"]] == ["rich-context:publication-a"]
    assert len(document["sources"]) == 2  # No dangling citation from a partially processed paper.
    assert len(document["quarantine"]) == 1
    rejection = document["quarantine"][0]
    assert rejection["kind"] == "publication" and rejection["location"] == "/@graph/3"
    assert rejection["source_id"] == "rich-context:fixed-sha"
    assert rejection["resource_path"] == "rich-context.jsonld"
    assert reason in rejection["reason"]


@pytest.mark.parametrize("links", [None, []])
def test_valid_unlinked_publications_retained_without_inventing_links(links):
    value = graph()
    value["@graph"][3]["cito:citesAsDataSource"] = links
    document = assert_valid(parse(value))
    assert document["coverage_report"]["unlinked_publications"] == 1
    assert len(document["papers"]) == 2 and len(document["relationships"]) == 1
    with pytest.raises(LiteratureSourceError):
        parse_rich_context(json.dumps(value).encode(), source_url=SOURCE_URL, revision="fixed-sha")


def test_standalone_invalid_provider_and_dataset_are_quarantined_separately():
    value = graph()
    value["@graph"][5]["dct:title"] = "invalid"
    document = assert_valid(parse(value))
    assert len(document["papers"]) == 2 and len(document["datasets"]) == 1
    assert len(document["providers"]) == 2
    assert {row["kind"] for row in document["quarantine"]} == {"dataset", "provider"}
    assert {row["location"] for row in document["quarantine"]} == {"/@graph/4", "/@graph/5"}
    rejection = next(row for row in document["quarantine"] if row["kind"] == "dataset")
    assert any(pointer["location"] == "/@graph/5" for pointer in rejection["dependency_locations"])


def test_failure_in_second_dependency_keeps_no_partial_publication_links():
    value = graph()
    value["@graph"][3]["cito:citesAsDataSource"] = [
        {"@id": VOCABULARY + "dataset-unused"},
        {"@id": VOCABULARY + "dataset-missing"},
    ]
    document = assert_valid(parse(value))
    assert len(document["relationships"]) == 1 and len(document["papers"]) == 1
    assert document["coverage_report"]["unused_dataset_nodes"] == 1
    assert document["quarantine"][0]["source_record_id"] == VOCABULARY + "publication-b"


@pytest.mark.parametrize("position", [0, 1, 2])
def test_duplicate_identifiers_never_choose_an_arbitrary_record(position):
    value = graph()
    value["@graph"].append(copy.deepcopy(value["@graph"][position]))
    document = assert_valid(parse(value))
    assert document["coverage_report"]["duplicate_graph_identifiers"] == 1
    if position == 2:
        assert [row["id"] for row in document["papers"]] == ["rich-context:publication-b"]
        assert document["coverage_report"]["rejected_publications"] == 2
    else:
        assert document["papers"] == [] and document["relationships"] == []
        assert document["coverage_report"]["rejected_publications"] == 2
    assert (
        len(
            [row for row in document["quarantine"] if "Duplicate graph identifier" in row["reason"]]
        )
        == 2
    )


def test_url_outer_whitespace_normalization_is_retained_and_pilot_stays_strict():
    value = graph()
    value["@graph"][1]["foaf:page"] = literal("  https://example.org/data \n")
    document = assert_valid(parse(value))
    dataset = next(row for row in document["datasets"] if row["id"] == "rich-context:dataset-1")
    assert dataset["url"] == "https://example.org/data"
    assert dataset["metadata_issues"] == [
        {
            "field": "foaf:page",
            "source_value": "  https://example.org/data \n",
            "normalized_value": "https://example.org/data",
            "method": "outer_whitespace_trim",
        }
    ]
    assert document["coverage_report"]["url_outer_whitespace_normalizations"] == 1
    assert len(document["papers"]) == 2
    with pytest.raises(LiteratureSourceError, match="URL"):
        parse_rich_context(json.dumps(value).encode(), source_url=SOURCE_URL, revision="fixed-sha")


def test_invalid_source_doi_visible_without_rejecting_or_inventing_identifier():
    value = graph()
    value["@graph"][2]["dct:identifier"] = literal("https://doi.org/RePEc:zbw:record")
    value["@graph"][3]["dct:identifier"] = literal("https://doi.org/10.1234/XYZ")
    document = assert_valid(parse(value))
    assert document["coverage_report"]["publications_with_doi"] == 1
    assert document["coverage_report"]["publications_with_identifier_issue"] == 1
    assert document["papers"][0]["source_identifier"] == "https://doi.org/RePEc:zbw:record"
    assert document["papers"][0]["doi"] is None


def test_malformed_nodes_accounted_without_type_errors():
    value = graph()
    value["@graph"] += [None, {}, {"@type": "Dataset", "@id": []}, {"@type": "ResearchPublication"}]
    document = assert_valid(parse(value))
    assert document["coverage_report"]["malformed_graph_nodes"] == 4
    assert document["coverage_report"]["rejected_dataset_records"] == 1
    assert document["coverage_report"]["rejected_publications"] == 1
    assert document["coverage_report"]["unused_dataset_nodes"] == 2
    assert any(row["source_record_id"] is None for row in document["quarantine"])


def test_no_publications_still_retains_valid_provider_catalogue():
    value = graph()
    value["@graph"] = [row for row in value["@graph"] if row["@type"] != "ResearchPublication"]
    document = assert_valid(parse(value))
    assert document["papers"] == [] and document["relationships"] == []
    assert len(document["datasets"]) == 2 and len(document["providers"]) == 3
    assert document["coverage_report"]["unused_dataset_nodes"] == 2


def test_repeat_import_is_deterministic_and_preserves_input():
    value = graph()
    before = copy.deepcopy(value)
    assert parse(value) == parse(value)
    assert value == before


def test_structural_json_and_remote_context_fail_entire_import():
    value = graph()
    value["@context"] = "https://example.org/context"
    with pytest.raises(LiteratureSourceError, match="remote contexts"):
        parse(value)
    with pytest.raises(LiteratureSourceError, match="Duplicate JSON key"):
        parse_rich_context_full(
            b'{"@context":{},"@context":{}}', source_url=SOURCE_URL, revision="x"
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"revision": ""},
        {"resource_path": "../raw"},
        {"source_url": "https://secret@example.org/raw"},
    ],
)
def test_full_source_arguments_validated_before_parsing(kwargs):
    arguments = {"source_url": SOURCE_URL, "revision": "fixed-sha", **kwargs}
    with pytest.raises(LiteratureSourceError):
        parse_rich_context_full(json.dumps(graph()).encode(), **arguments)


def test_actual_pinned_corpus_all_entities_accounted_and_offline_portable(tmp_path):
    package = Path(__file__).resolve().parents[1] / "examples/literature/pilot.ssdu-literature.zip"
    if not package.is_file():
        pytest.skip("Source-only publication excludes the captured Rich Context corpus")
    with zipfile.ZipFile(package) as archive:
        raw = archive.read("resources/rich-context.jsonld")
    revision = "4d5347d8d1ac2693901966d6dd6905ba14133f89"
    document = parse_rich_context_full(
        raw,
        source_url=f"https://raw.githubusercontent.com/Coleridge-Data-For-Impact/rclc/{revision}/corpus.jsonld",
        revision=revision,
    )
    validated = assert_valid(document)
    report = validated["coverage_report"]
    assert len(raw) == 3_879_137
    assert report["source_publications"] == 1604
    assert report["imported_publications"] == 1603 and report["rejected_publications"] == 1
    assert report["imported_dataset_records"] == report["source_dataset_nodes"] == 208
    assert report["imported_provider_records"] == report["source_provider_nodes"] == 310
    assert report["relationships"] == 1849 and report["publications_with_doi"] == 1573
    assert report["publications_without_doi"] == 30
    assert report["unused_dataset_nodes"] == 0
    assert report["providers_unreferenced_by_imported_datasets"] == 203
    assert report["url_outer_whitespace_normalizations"] == 1
    rejection = document["quarantine"][0]
    assert rejection["location"] == "/@graph/5418"
    assert rejection["source_record_id"] == VOCABULARY + "publication-618e73b7a15fbdd9f814"
    assert "URL" in rejection["reason"] and len(rejection["dependency_locations"]) == 2
    assert (
        len(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
        < MAX_DOCUMENT_BYTES
    )
    original = LiteratureStore(tmp_path / "original")
    snapshot = original.publish(document, {"rich-context.jsonld": raw})
    export = tmp_path / "full.ssdu-literature.zip"
    original.export(snapshot, export)
    fresh = LiteratureStore(tmp_path / "fresh")
    assert fresh.import_bundle(export) == snapshot
    assert original.read(snapshot) == fresh.read(snapshot) == validated
