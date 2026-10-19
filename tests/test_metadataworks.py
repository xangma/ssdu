"""Invented protocol fixtures; no source metadata or provider requests."""

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

from ssdu.adapters.metadataworks import MetadataWorksBackend
from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient, Resource
from ssdu.metadata_connectors import (
    MetadataConnectorRegistration,
    MetadataConnectorRegistry,
    get_metadata_connector,
    list_metadata_connectors,
    register_metadata_connector,
)
from ssdu.metadata_graph import entity_id, validate_entity, validate_link
from ssdu.models import RetrievalError
from ssdu.providers import get_provider
from ssdu.rate_limit import RateLimiter


def registration(name="invented"):
    return MetadataConnectorRegistration(
        name,
        "https://metadata.example.invalid/api",
        "https://example.invalid/catalogue",
        frozenset({"metadata.example.invalid"}),
        "Invented public metadata only; restricted records are unavailable",
    )


class FixtureClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        body = json.dumps(self.data).encode()
        return Resource(
            body,
            str(httpx.Request("GET", url, params=params).url),
            "GET",
            None,
            "application/json",
            {},
            captured_at="2026-01-01T00:00:00+00:00",
            checked_at="2026-01-01T00:00:00+00:00",
        )


def task(kind="element", **overrides):
    fields = {"kind": kind, "native_id": "020", "origin": "02", "version": None}
    if kind == "element":
        fields.update(dataset_id="001", table_id="010")
    if kind in {"dictionary", "structure"}:
        fields.update(native_id="001", page_number=0, page_size=1)
    return {**fields, **overrides}


def element():
    return {
        "id": "020",
        "elementType": "DataElement",
        "name": "COMMUNITY",
        "description": "An entirely invented response variable.",
        "unit": "category",
        "dataType": {
            "name": "InventedCommunityType",
            "basedOn": "Varchar",
            "enumerations": [
                {"name": "01", "description": "Urban", "count": "123", "isMissing": False},
                {"name": "99", "description": "Not stated", "isMissing": True},
            ],
        },
        "question": {"id": "Q01", "text": "Which invented community do you belong to?"},
        "metadata": [{"key": "01", "value": "123"}],
    }


def page(content, *, number=0, total=2, size=1):
    return {
        "content": content,
        "totalSize": total,
        "totalPages": (total + size - 1) // size,
        "pageNumber": number,
        "size": size,
        "numberOfElements": len(content),
    }


def test_metadata_registry_is_independent_and_immutable():
    assert [entry.id for entry in list_metadata_connectors()] == [
        "adr_uk",
        "nisra_secure",
        "research_data_scotland",
    ]
    entry = get_metadata_connector("nisra_secure")
    assert entry.headers == {"X-API-Version": "2.0"}
    assert not hasattr(entry, "normalize")
    assert not hasattr(entry, "fetch_dataset")
    with pytest.raises(FrozenInstanceError):
        entry.api_base = "https://elsewhere.invalid"
    with pytest.raises(TypeError):
        entry.headers["X-API-Version"] = "1.0"
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("nisra_secure")
    assert isinstance(entry.backend(), MetadataWorksBackend)


def test_custom_registry_requires_explicit_configuration_and_preserves_builtins():
    entry = registration()
    registry = register_metadata_connector(entry)
    assert get_metadata_connector("invented", registry=registry) is entry
    assert get_metadata_connector(entry) is entry
    assert "invented" not in [entry.id for entry in list_metadata_connectors()]
    assert len(entry.fingerprint) == 64 and len(entry.cache_scope) < 500
    assert entry.fingerprint == registration().fingerprint
    assert entry.fingerprint != registration("other").fingerprint
    with pytest.raises(ValueError, match="Duplicate"):
        register_metadata_connector(entry, registry=registry)
    with pytest.raises(TypeError):
        MetadataConnectorRegistry([{}])


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "Not Valid"},
        {"api_base": "http://metadata.example.invalid/api"},
        {"api_base": "https://evil.invalid/api"},
        {"api_base": "https://user:secret@metadata.example.invalid/api"},
        {"api_base": "https://metadata.example.invalid:8080/api"},
        {"api_base": "https://metadata.example.invalid/api?headers=override"},
        {"allowed_hosts": frozenset({"*.example.invalid"})},
        {"allowed_hosts": {"metadata.example.invalid"}},
        {"scope": ""},
        {"family": "unreviewed"},
        {"api_version": "1.0"},
    ],
)
def test_registration_rejects_unreviewed_configuration(changes):
    fields = registration().__dict__
    with pytest.raises(ValueError):
        MetadataConnectorRegistration(**{**fields, **changes})


def test_explicit_questions_and_codes_preserve_native_meaning():
    backend = registration().backend()
    client = FixtureClient(element())
    result = backend.fetch(task(), client)
    assert len(client.calls) == 1
    assert client.calls[0][0].endswith("/dataElement/020")
    assert client.calls[0][1] == {"origin": "02"}
    entities = result["entities"]
    assert {row["kind"] for row in entities} == {"variable", "question", "code_list", "code"}
    question = next(row for row in entities if row["kind"] == "question")
    assert question["native_id"] == "Q01"
    assert question["attributes"]["identity_basis"] == "source_identifier"
    assert question["label"] == element()["question"]["text"]
    codes = [row for row in entities if row["kind"] == "code"]
    assert [row["attributes"]["value"] for row in codes] == ["01", "99"]
    assert codes[1]["attributes"]["isMissing"] is True
    assert codes[1]["label"] == "Not stated"
    assert "123" not in json.dumps(entities)
    digest = hashlib.sha256(json.dumps(element()).encode()).hexdigest()
    for row in entities:
        validate_entity(row)
        assert row["provenance"][0]["body_sha256"] == digest
    for link in result["links"]:
        validate_link(link)
    assert next(row for row in entities if row["kind"] == "variable")["native_id"] == "020"


def test_never_infers_questions_from_variable_description():
    data = element()
    del data["question"]
    data["description"] = "How are you? This is a variable description, not an instrument."
    result = registration().backend().fetch(task(), FixtureClient(data))
    assert not any(row["kind"] == "question" for row in result["entities"])
    variable = result["entities"][0]
    assert variable["field_status"]["questions"] == "not_provided"


def test_question_wording_without_identifier_uses_explicit_source_path_identity():
    data = element()
    data["question"] = {"text": "A supplied question without a native ID?"}
    result = registration().backend().fetch(task(), FixtureClient(data))
    row = next(row for row in result["entities"] if row["kind"] == "question")
    assert row["native_id"] == "element/020/question"
    assert row["attributes"]["source_native_id"] is None
    assert row["attributes"]["identity_basis"] == "source_path"
    assert row["provenance"][0]["pointer"] == "/question"


def test_same_native_ids_in_distinct_origins_catalogues_and_releases_are_distinct():
    first = registration().backend().fetch(task(), FixtureClient(element()))["entities"][0]
    other_origin = (
        registration().backend().fetch(task(origin="03"), FixtureClient(element()))["entities"][0]
    )
    other_catalogue = (
        registration("other").backend().fetch(task(), FixtureClient(element()))["entities"][0]
    )
    assert len({first["id"], other_origin["id"], other_catalogue["id"]}) == 3
    assert entity_id("invented", "02", "release", "001", "1.0.0") != entity_id(
        "invented", "02", "release", "001", "2.0.0"
    )


def test_release_descriptors_do_not_invent_historical_dictionary_requests():
    data = {
        "id": "001",
        "origin": {"id": "02"},
        "summary": {"title": "Invented study"},
        "versionHistory": [
            {"semanticVersion": version, "dataModelId": "001"} for version in ("1.0.0", "2.0.0")
        ],
        "documentation": {"associatedMedia": [{"url": "https://example.invalid/dictionary.xlsx"}]},
    }
    result = registration().backend().fetch(task("dataset", native_id="001"), FixtureClient(data))
    releases = [row for row in result["entities"] if row["kind"] == "release"]
    assert {row["version"] for row in releases} == {"1.0.0", "2.0.0"}
    assert {row["attributes"]["capture_depth"] for row in releases} == {"descriptor_only"}
    assert {item["kind"] for item in result["next_tasks"]} == {"structure", "dictionary"}
    assert all(item["version"] is None for item in result["next_tasks"])
    assert len([row for row in result["coverage"] if row["status"] == "partial"]) == 2


def test_dictionary_pagination_is_zero_based_and_repeated_content_is_partial():
    backend = registration().backend()
    row = {"id": "020", "parentId": "010", "dataType": "dataElement"}
    client = FixtureClient(page([row]))
    result = backend.fetch(task("dictionary"), client)
    assert client.calls[0][1] == {"originId": "02", "page": 0, "size": 1}
    next_page = next(item for item in result["next_tasks"] if item["kind"] == "dictionary")
    assert next_page["page_number"] == 1
    assert next_page["seen_page_hashes"]
    repeated = backend.fetch(next_page, FixtureClient(page([row], number=1)))
    assert repeated["entities"] == repeated["next_tasks"] == []
    assert repeated["coverage"][0]["status"] == "partial"
    assert "repeated" in repeated["coverage"][0]["reason"]


def test_empty_dictionary_is_not_inferred_from_missing_content():
    backend = registration().backend()
    valid = backend.fetch(
        task("dictionary"), FixtureClient({"totalSize": 0, "size": 1, "pageNumber": 0})
    )
    assert valid["coverage"][0]["observed"] == 0
    with pytest.raises(RetrievalError, match="content"):
        backend.fetch(task("dictionary"), FixtureClient({"size": 1, "pageNumber": 0}))


def test_source_count_and_identity_changes_fail_closed_between_pages():
    backend = registration().backend()
    first = backend.fetch(task("dictionary"), FixtureClient(page([{"id": "020"}])))
    next_page = next(item for item in first["next_tasks"] if item["kind"] == "dictionary")
    with pytest.raises(RetrievalError, match="count changed"):
        backend.fetch(next_page, FixtureClient(page([{"id": "021"}], number=1, total=3)))
    with pytest.raises(RetrievalError, match="different dataset or origin"):
        backend.fetch(
            task("dictionary"),
            FixtureClient(
                {**page([{"id": "020"}]), "resourceIdentifier": {"id": "other", "originId": "02"}}
            ),
        )


def test_duplicate_native_rows_do_not_count_towards_completeness():
    data = page([{"id": "020"}, {"id": "020"}], total=2, size=2)
    with pytest.raises(RetrievalError, match="repeats a native record"):
        registration().backend().fetch(task("dictionary", page_size=2), FixtureClient(data))


def test_unpaginated_structure_is_accepted_only_when_its_entire_scope_is_bounded():
    backend = registration().backend()
    data = page([{"id": "010", "name": "Invented table"}], total=1)
    data.update(size=-1, totalPages=-1)
    result = backend.fetch(task("structure"), FixtureClient(data))
    assert len(result["entities"]) == 1
    assert result["coverage"][0]["status"] == "exhausted"
    assert result["entities"][0]["version"] is None
    assert result["entities"][0]["field_status"]["version"] == "not_provided"
    with pytest.raises(RetrievalError, match="page size"):
        backend.fetch(task("structure"), FixtureClient({**data, "totalSize": 2}))
    with pytest.raises(RetrievalError, match="page size"):
        backend.fetch(task("dictionary"), FixtureClient(data))


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "not a dictionary"},
        {"content": [None]},
        {"totalSize": True},
        {"size": 100},
        {"pageNumber": 1},
        {"numberOfElements": 2},
        {"content": [], "totalSize": 2},
    ],
)
def test_malformed_or_ignored_pagination_fails_closed(changes):
    data = page([{"id": "020"}])
    with pytest.raises(RetrievalError):
        registration().backend().fetch(task("dictionary"), FixtureClient({**data, **changes}))


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "download"},
        {"url": "https://evil.invalid"},
        {"version": "2.0.0"},
        {"native_id": ""},
        {"page_size": 0},
        {"origin": True},
        {"seen_page_hashes": ["bad"]},
    ],
)
def test_untrusted_tasks_cannot_supply_routes_or_relabel_versions(changes):
    with pytest.raises(RetrievalError):
        registration().backend().validate_task({**task("dictionary"), **changes})


def test_one_http_call_task_is_cached_with_protocol_header_and_replays_offline(tmp_path):
    entry = registration()
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["X-API-Version"] == "2.0"
        return httpx.Response(200, json=element())

    with (
        HttpClient(
            transport=httpx.MockTransport(respond),
            allowed_hosts=entry.allowed_hosts,
            headers=dict(entry.headers),
            rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
        ) as transport,
        CachedClient(tmp_path, client=transport, allowed_hosts=entry.allowed_hosts) as client,
        client.scope(entry.cache_scope),
    ):
        first = entry.backend().fetch(task(), client)
    assert len(calls) == 1
    with (
        CachedClient(tmp_path, offline=True, allowed_hosts=entry.allowed_hosts) as offline,
        offline.scope(entry.cache_scope),
    ):
        replay = entry.backend().fetch(task(), offline)
    assert replay == first
    with (
        CachedClient(tmp_path, offline=True, allowed_hosts=entry.allowed_hosts) as offline,
        offline.scope("another-protocol"),
        pytest.raises(RetrievalError, match="No retained"),
    ):
        entry.backend().fetch(task(), offline)


def test_variable_identity_mismatch_and_duplicate_codes_fail_closed():
    data = element()
    data["id"] = "021"
    with pytest.raises(RetrievalError, match="different native"):
        registration().backend().fetch(task(), FixtureClient(data))
    data = element()
    data["dataType"]["enumerations"].append(copy.deepcopy(data["dataType"]["enumerations"][0]))
    with pytest.raises(RetrievalError, match="repeats an enumeration"):
        registration().backend().fetch(task(), FixtureClient(data))


@pytest.mark.parametrize("origin_fields", [{"origin": {"id": "other"}}, {"originId": "other"}])
def test_element_details_reject_an_explicit_different_origin(origin_fields):
    data = {**element(), **origin_fields}
    with pytest.raises(RetrievalError, match="different origin"):
        registration().backend().fetch(task(), FixtureClient(data))


def test_element_origin_can_be_omitted_but_never_fabricates_parent_evidence():
    result = registration().backend().fetch(task(), FixtureClient(element()))
    assert result["entities"][0]["origin"] == "02"
    assert not any(link["relation"] == "has_variable" for link in result["links"])


def test_variable_parent_edges_cite_the_listing_that_contains_the_relationship():
    backend = registration().backend()
    dictionary = page([{"id": "020", "parentId": "010"}], total=1)
    dictionary_client = FixtureClient(dictionary)
    result = backend.fetch(task("dictionary"), dictionary_client)
    edge = next(link for link in result["links"] if link["relation"] == "has_variable")
    assert edge["source"] == entity_id("invented", "02", "table", "010")
    assert edge["target"] == entity_id("invented", "02", "variable", "020")
    assert edge["provenance"][0]["pointer"] == "/content/0"
    assert "/structuralMetadata/dataDictionary/001?" in edge["provenance"][0]["url"]
    assert (
        edge["provenance"][0]["body_sha256"]
        == hashlib.sha256(json.dumps(dictionary).encode()).hexdigest()
    )
    structure = page(
        [{"id": "010", "name": "Invented table", "dataElements": [{"id": "020"}]}], total=1
    )
    result = backend.fetch(task("structure"), FixtureClient(structure))
    edge = next(link for link in result["links"] if link["relation"] == "has_variable")
    assert edge["provenance"][0]["pointer"] == "/content/0/dataElements/0"
    assert edge["source"] == entity_id("invented", "02", "table", "010")
    assert edge["target"] == entity_id("invented", "02", "variable", "020")


def test_conflicting_variable_parent_in_nested_structure_fails_closed():
    structure = page([{"id": "010", "dataElements": [{"id": "020", "parentId": "other"}]}], total=1)
    with pytest.raises(RetrievalError, match="different containing table"):
        registration().backend().fetch(task("structure"), FixtureClient(structure))
