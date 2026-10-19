"""Synthetic deep metadata preserves source identity, unknowns and evidence."""

import copy
import hashlib

import jsonschema
import pytest

from ssdu.metadata_contract import CONTRACT_VERSION, MetadataError
from ssdu.metadata_graph import (
    ENTITY_KINDS,
    GRAPH_CONTRACT_VERSION,
    contract_schema,
    entity_id,
    make_entity,
    make_link,
    validate_entity,
    validate_link,
)


def evidence():
    return [
        {
            "url": "https://synthetic.example/catalogue",
            "body_sha256": hashlib.sha256(b"{}").hexdigest(),
            "pointer": "",
            "captured_at": "2026-10-19T10:00:00Z",
            "checked_at": "2026-10-19T11:00:00+01:00",
        }
    ]


def synthetic_entity(kind="variable", native_id="v1", **kwargs):
    return make_entity("invented", "origin-a", kind, native_id, provenance=evidence(), **kwargs)


def test_additive_contract_keeps_old_contract_unchanged_and_publishes_schema():
    assert CONTRACT_VERSION == "1.1.0"
    assert GRAPH_CONTRACT_VERSION == "1.0.0"
    schema = contract_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    entities = [synthetic_entity(kind, kind) for kind in ENTITY_KINDS]
    for entity in entities:
        validator.validate(entity)
        assert validate_entity(entity) == entity
    link = make_link(entities[0]["id"], entities[1]["id"], "has_variable", provenance=evidence())
    validator.validate(link)
    assert validate_link(link) == link


def test_identity_is_qualified_by_catalogue_origin_kind_native_id_and_version():
    baseline = entity_id("invented", "origin-a", "variable", "001")
    ids = {
        baseline,
        entity_id("other", "origin-a", "variable", "001"),
        entity_id("invented", "origin-b", "variable", "001"),
        entity_id("invented", "origin-a", "question", "001"),
        entity_id("invented", "origin-a", "variable", "1"),
        entity_id("invented", "origin-a", "variable", "001", "1"),
        entity_id("invented", "origin-a", "variable", "001", "2"),
    }
    assert len(ids) == 7
    assert (
        synthetic_entity(label="Invented label")["id"]
        == synthetic_entity(label="Updated label")["id"]
    )


def test_unknown_metadata_remains_null_and_arbitrary_native_attributes_survive():
    native = {"native_datatype": None, "restrictions": {"publication": "unknown"}, "codes": []}
    item = synthetic_entity(
        description=None,
        attributes=native,
        field_status={"description": "not_provided", "questions": "not_requested"},
    )
    assert item["version"] is None and item["description"] is None
    assert item["attributes"] == native
    native["restrictions"]["publication"] = "changed"
    assert item["attributes"]["restrictions"]["publication"] == "unknown"
    unknown_clocks = copy.deepcopy(item)
    unknown_clocks["provenance"][0].update(captured_at=None, checked_at=None)
    assert validate_entity(unknown_clocks)["provenance"][0]["checked_at"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "a" * 64),
        ("kind", "distribution"),
        ("kind", []),
        ("catalogue", ""),
        ("native_id", 42),
        ("version", 1),
        ("version", ""),
        ("label", None),
        ("attributes", []),
        ("attributes", {"bad": float("nan")}),
        ("provenance", []),
        ("field_status", {"description": "unknown"}),
        ("field_status", {"description": []}),
    ],
)
def test_invalid_entity_fields_fail_closed(field, value):
    entity = synthetic_entity()
    entity[field] = value
    with pytest.raises(MetadataError):
        validate_entity(entity)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "http://synthetic.example/catalogue"),
        ("url", "https://user:secret@synthetic.example/catalogue"),
        ("url", "https://synthetic.example:444/catalogue"),
        ("url", "https://synthetic.example/catalogue#section"),
        ("body_sha256", "short"),
        ("pointer", "not-a-pointer"),
        ("pointer", "/invalid~2escape"),
        ("captured_at", "2026-10-19"),
        ("checked_at", "2026-10-19T00:00:00"),
    ],
)
def test_invalid_evidence_fails_closed(field, value):
    entity = synthetic_entity()
    entity["provenance"][0][field] = value
    with pytest.raises(MetadataError):
        validate_entity(entity)


def test_normalized_evidence_deduplicates_without_losing_pointers():
    refs = evidence()
    refs += [dict(refs[0]), {**refs[0], "pointer": "/a~1b/~0c/0"}]
    item = make_entity("synthetic", "origin", "question", "q1", provenance=refs)
    assert len(item["provenance"]) == 2
    item["unexpected"] = "not versioned"
    with pytest.raises(MetadataError):
        validate_entity(item)


@pytest.mark.parametrize("relation", ["", "has variable", "HasVariable", None, []])
def test_relationships_require_explicit_machine_readable_types(relation):
    with pytest.raises(MetadataError):
        make_link("a" * 64, "b" * 64, relation, provenance=evidence())
