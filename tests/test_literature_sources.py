"""Source adapters preserve assertions and reject malformed input without networking."""

import copy
import json
import socket

import pytest

from ssdu.literature_sources import (
    RICH_CONTEXT,
    VOCABULARY,
    LiteratureSourceError,
    enrich_openalex,
    normalize_doi,
    parse_rich_context,
    reconstruct_abstract,
)

SOURCE_URL = "https://example.org/corpus.jsonld"


def literal(value):
    return {"@value": value}


def graph():
    return {
        "@context": RICH_CONTEXT,
        "@graph": [
            {
                "@id": VOCABULARY + "provider-1",
                "@type": "Provider",
                "dct:title": literal("Welsh provider"),
            },
            {
                "@id": VOCABULARY + "dataset-1",
                "@type": "Dataset",
                "dct:title": literal("Wales 2019 programme"),
                "dct:publisher": literal("provider-1"),
                "foaf:page": literal("https://example.org/data"),
            },
            {
                "@id": VOCABULARY + "publication-z",
                "@type": "ResearchPublication",
                "dct:title": literal("Original title"),
                "dct:identifier": literal("https://doi.org/10.1234/ABC"),
                "foaf:page": literal("https://example.org/paper"),
                "cito:citesAsDataSource": {"@id": VOCABULARY + "dataset-1"},
            },
            {
                "@id": VOCABULARY + "publication-a",
                "@type": "ResearchPublication",
                "dct:title": literal("A publication"),
                "cito:citesAsDataSource": [{"@id": VOCABULARY + "dataset-1"}],
            },
            {"@id": VOCABULARY + "ignored", "@type": "Unknown", "custom": "retained upstream only"},
        ],
    }


def parse(value=None, **kwargs):
    return parse_rich_context(
        json.dumps(value or graph()).encode(), source_url=SOURCE_URL, revision="fixed-sha", **kwargs
    )


def work(**kwargs):
    return {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1234/abc",
        "title": "Alternate title",
        "publication_date": "2020-01-02",
        "abstract_inverted_index": {"Data": [0], "used": [1], "here.": [2]},
        **kwargs,
    }


def enrich(document=None, records=None, **kwargs):
    return enrich_openalex(
        document or parse(),
        records or [work()],
        source_url="https://api.openalex.org/works/https://doi.org/10.1234/abc",
        resource_path="raw/openalex.json",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Pure literature adapter attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.mark.parametrize(
    "raw",
    [
        "10.1234/ABC",
        " DOI:10.1234/ABC ",
        "https://doi.org/10.1234%2FABC",
        "http://dx.doi.org/10.1234/ABC",
    ],
)
def test_doi_normalization(raw):
    assert normalize_doi(raw) == "10.1234/abc"
    assert normalize_doi(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.org/10.1234/abc",
        "https://doi.org:abc/10.1234/abc",
        "https://user@doi.org/10.1234/abc",
        "https://doi.org/10.1234/abc?secret=x",
        "https://doi.org/10.1234/abc#fragment",
        "10.1234/space here",
        "10.1234/a`b",
        "10.1234/%26",
        "RePEc:abc",
        "",
        10,
    ],
)
def test_doi_rejects_non_doi_and_unsafe_wrappers(raw):
    with pytest.raises(LiteratureSourceError):
        normalize_doi(raw)


def test_graph_deterministic_selection_linkage_and_unknown_metadata():
    document = parse(max_papers=1)
    assert document["papers"][0]["id"] == "rich-context:publication-a"
    assert document["selection"]["method"] == "sorted_ids"
    dataset = document["datasets"][0]
    assert dataset["provider"] == "Welsh provider"
    assert dataset["provider_id"] == document["providers"][0]["id"]
    assert dataset["coverage"] == {} and dataset["version"] is None
    assert dataset["catalogue_entry_kind"] == "source_catalogue_entry"
    relation = document["relationships"][0]
    assert relation["relationship_type"] == "source_asserted_use"
    assert relation["review"]["status"] == "source_reviewed"
    assert not relation["review"]["independently_human_validated"]
    assert relation["evidence"]["location"] == "/@graph/3/cito:citesAsDataSource/0"
    for row in document["papers"] + document["datasets"] + document["relationships"]:
        assert row["references"] and all(ref["title"] and ref["url"] for ref in row["references"])


def test_explicit_selection_accepts_native_normalized_and_full_ids():
    assert parse(publication_ids=["publication-z"])["papers"][0]["doi"] == "10.1234/abc"
    expected = parse(publication_ids=["publication-z"])
    assert parse(publication_ids=[VOCABULARY + "publication-z"]) == expected
    assert parse(publication_ids=["rich-context:publication-z"]) == expected
    a = parse(publication_ids=["publication-z", "publication-a"])
    b = parse(publication_ids=["publication-a", "publication-z"])
    assert a == b


@pytest.mark.parametrize(
    "identifiers",
    [
        [],
        ["publication-missing"],
        ["publication-z", "publication-z"],
        ["dataset-1"],
        "publication-z",
    ],
)
def test_bad_selections_reject(identifiers):
    with pytest.raises(LiteratureSourceError):
        parse(publication_ids=identifiers)


@pytest.mark.parametrize("limit", [0, 101, True, 2.5])
def test_selection_bounds(limit):
    with pytest.raises(LiteratureSourceError):
        parse(max_papers=limit)


def test_duplicates_and_remote_context_reject():
    value = graph()
    value["@graph"].append(copy.deepcopy(value["@graph"][0]))
    with pytest.raises(LiteratureSourceError, match="Duplicate graph identifier"):
        parse(value)
    value = graph()
    value["@context"] = "https://example.org/context"
    with pytest.raises(LiteratureSourceError, match="remote contexts"):
        parse(value)
    with pytest.raises(LiteratureSourceError, match="Duplicate JSON key"):
        parse_rich_context(b'{"@context":{},"@context":{}}', source_url=SOURCE_URL, revision="x")


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda v: v["@graph"][2].update(
                {"cito:citesAsDataSource": {"@id": VOCABULARY + "dataset-missing"}}
            ),
            "Broken referenced dataset",
        ),
        (
            lambda v: v["@graph"][1].update({"dct:publisher": literal("provider-missing")}),
            "Broken referenced provider",
        ),
        (lambda v: v["@graph"][2].update({"cito:citesAsDataSource": []}), "Invalid dataset links"),
        (
            lambda v: v["@graph"][2].update(
                {"cito:citesAsDataSource": [{"@id": VOCABULARY + "dataset-1"}] * 2}
            ),
            "Duplicate dataset link",
        ),
        (lambda v: v["@graph"][2].update({"dct:title": "plain string"}), "must be a literal"),
    ],
)
def test_selected_link_validation(mutation, message):
    value = graph()
    mutation(value)
    with pytest.raises(LiteratureSourceError, match=message):
        parse(value)


def test_source_non_doi_wrappers_are_visible_without_false_doi():
    value = graph()
    value["@graph"][2]["dct:identifier"] = literal("https://doi.org/RePEc:zbw:bubdp1:5197")
    paper = parse(value, publication_ids=["publication-z"])["papers"][0]
    assert paper["doi"] is None
    assert paper["source_identifier"].endswith("RePEc:zbw:bubdp1:5197")
    assert paper["identifier_issue"]


def test_provider_reference_form_supported():
    value = graph()
    value["@graph"][1]["dct:publisher"] = {"@id": VOCABULARY + "provider-1"}
    assert parse(value)["datasets"][0]["provider"] == "Welsh provider"


def test_abstract_reconstruction_repeated_tokens_and_null():
    assert reconstruct_abstract({"the": [0, 2], "data": [1], "paper": [3]}) == "the data the paper"
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


@pytest.mark.parametrize(
    "index",
    [
        {"a": [0], "b": [0]},
        {"a": [1]},
        {"a": [-1]},
        {"a": [True]},
        {"a": [20_000]},
        {"a": []},
        {"a": "0"},
        {"": [0]},
        [],
        {"a": [0, 2]},
    ],
)
def test_malformed_abstract_positions_fail(index):
    with pytest.raises(LiteratureSourceError):
        reconstruct_abstract(index)


def test_enrichment_exact_doi_fields_provenance_and_idempotence():
    document = parse()
    original = copy.deepcopy(document)
    enriched = enrich(document)
    assert document == original
    paper = next(row for row in enriched["papers"] if row["doi"])
    assert paper["title"] == "Original title"
    assert paper["abstract"] == "Data used here."
    assert paper["published_at"] == "2020-01-02"
    provenance = paper["metadata_provenance"][0]
    assert provenance["matched_by"] == "doi"
    assert provenance["applied_fields"] == ["abstract", "published_at"]
    assert provenance["conflicting_fields"] == ["title"]
    assert provenance["values"]["title"] == "Alternate title"
    source = next(row for row in enriched["sources"] if row["id"] == provenance["source_id"])
    assert source["resource_paths"] == ["raw/openalex.json"]
    assert source["metadata_licence"] == "CC0"
    assert paper["references"][-1]["id"] == source["id"]
    assert enrich(enriched) == enriched
    assert next(row for row in enriched["papers"] if row["doi"] is None)["abstract"] is None


def test_no_title_matching_and_missing_abstract_preserved():
    result = enrich(records=[work(doi="10.1234/other", title="Original title")])
    assert result == parse()
    result = enrich(records=[work(abstract_inverted_index=None)])
    assert next(row for row in result["papers"] if row["doi"])["abstract"] is None


def test_known_abstract_not_silently_overwritten():
    document = parse()
    next(row for row in document["papers"] if row["doi"])["abstract"] = "Known abstract."
    paper = next(row for row in enrich(document)["papers"] if row["doi"])
    assert paper["abstract"] == "Known abstract."
    assert "abstract" in paper["metadata_provenance"][0]["conflicting_fields"]


@pytest.mark.parametrize(
    "record",
    [
        work(id="https://evil.org/W123"),
        work(publication_date="2020-13-01"),
        work(title=123),
        work(abstract_inverted_index={"word": [2]}),
    ],
)
def test_matched_openalex_record_validation(record):
    with pytest.raises(LiteratureSourceError):
        enrich(records=[record])


def test_duplicate_doi_and_conflicting_capture_reject():
    with pytest.raises(LiteratureSourceError, match="Duplicate OpenAlex DOI"):
        enrich(records=[work(), work()])
    document = enrich()
    with pytest.raises(LiteratureSourceError, match="Conflicting OpenAlex work metadata"):
        enrich(document, records=[work(title="Changed")])


@pytest.mark.parametrize(
    "resource_path", ["../raw.json", "/raw.json", "raw\\file.json", "raw//file.json", ""]
)
def test_resource_keys_are_safe_relative_paths(resource_path):
    with pytest.raises(LiteratureSourceError, match="resource_path"):
        parse(resource_path=resource_path)
    with pytest.raises(LiteratureSourceError, match="resource_path"):
        enrich_openalex(
            parse(),
            [work()],
            source_url="https://api.openalex.org/works/W123",
            resource_path=resource_path,
        )


def test_enriched_document_matches_registry_contract():
    from ssdu.literature_store import validate_document

    validate_document(enrich(), resource_names={"rich-context.jsonld", "raw/openalex.json"})
