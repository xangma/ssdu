"""Invented metadata retains identity, unknowns, clocks and attributed evidence."""

import copy
import math

import pytest

from ssdu.metadata_contract import (
    CONTRACT_VERSION,
    MetadataError,
    canonical,
    parse_json,
    record_version,
    validate_document,
    validate_record,
)


def synthetic_document():
    """Two invented series with unrelated dimensions, not copied provider metadata."""
    reference = {
        "title": "Invented provider catalogue",
        "url": "https://example.org/catalogue",
        "resource_paths": ["raw/catalogue.json"],
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "captured_at": "2026-10-15T12:00:00Z",
        "providers": [
            {
                "provider": "nomis",
                "scope": {"query": "", "locale": "en"},
                "completeness": {
                    "status": "complete",
                    "catalogue_complete": True,
                    "descriptions_complete": False,
                    "discovered_count": 2,
                    "described_count": 1,
                    "inventory_authoritative": False,
                },
                "freshness": {
                    "captured_at": "2026-10-14T09:00:00Z",
                    "last_checked_at": None,
                    "provider_modified_at": "2026-09-01",
                },
                "references": [reference],
                "raw_resources": ["raw/catalogue.json"],
            }
        ],
        "records": [
            {
                "provider": "nomis",
                "native_id": "FAKE_001",
                "dataset_id": "nomis:FAKE_001",
                "title": "Invented household count",
                "description": None,
                "kind": "dataset",
                "dimensions": [{"id": "area", "label": "Invented area", "count": 2}],
                "measures": [],
                "distributions": [],
                "coverage": None,
                "release": None,
                "access": None,
                "licence": None,
                "captured_at": "2026-10-14T09:00:00Z",
                "last_checked_at": None,
                "provider_modified_at": "2026-09-01",
                "references": [reference],
                "raw_resources": ["raw/catalogue.json"],
                "source_metadata": {"native": {"unmapped": ["kept", 3, None]}},
            },
            {
                "provider": "nomis",
                "native_id": "FAKE_002",
                "dataset_id": "nomis:FAKE_002",
                "title": "Invented learning series",
                "kind": "series",
                "references": [reference],
                "raw_resources": ["raw/catalogue.json"],
                "derived_evidence": [
                    {
                        "method": "manual_synthetic_annotation",
                        "asserted_by": "Fixture author",
                        "summary": "Invented training theme",
                        "references": [reference],
                    }
                ],
            },
        ],
    }


def test_contract_retains_unknowns_native_metadata_and_separate_capture_clocks():
    original = synthetic_document()
    result = validate_document(original, ["raw/catalogue.json"])
    assert result == original
    assert result is not original
    row = result["records"][0]
    assert row["description"] is None and row["licence"] is None
    assert row["source_metadata"]["native"]["unmapped"] == ["kept", 3, None]
    assert row["provider_modified_at"] == "2026-09-01"
    assert row["captured_at"] == "2026-10-14T09:00:00Z"
    assert row["last_checked_at"] is None
    assert result["captured_at"] == "2026-10-15T12:00:00Z"
    assert result["records"][1]["derived_evidence"][0]["asserted_by"] == "Fixture author"


def test_record_revision_is_stable_across_capture_clocks_and_resource_locations():
    first = synthetic_document()["records"][0]
    second = copy.deepcopy(first)
    second["captured_at"] = "2026-10-15T12:00:00Z"
    second["last_checked_at"] = "2026-10-15T12:00:00Z"
    second["raw_resources"] = ["raw/later.json"]
    second["references"][0]["resource_paths"] = ["raw/later.json"]
    assert record_version(first) == record_version(second)
    second["source_metadata"]["native"]["unmapped"][1] = 4
    assert record_version(first) != record_version(second)


def test_canonical_record_order_and_native_ids_remain_stable():
    document = synthetic_document()
    document["records"].reverse()
    first = validate_document(document)
    document["records"].reverse()
    assert canonical(first) == canonical(validate_document(document))
    row = document["records"][0]
    row["native_id"] = "https://example.org/dataset/a%2Fb#series"
    row["dataset_id"] = "nomis:" + row["native_id"]
    assert validate_record(row)["native_id"] == row["native_id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(contract_version="2.0.0"),
        lambda d: d.update(unreviewed_core={}),
        lambda d: d.update(captured_at="2026-10-15"),
        lambda d: d.update(captured_at="2026-10-15T12:00:00"),
        lambda d: d.update(previous_snapshot_id="short"),
        lambda d: d.update(records={}),
        lambda d: d.update(providers=[]),
        lambda d: d["records"].append(copy.deepcopy(d["records"][0])),
        lambda d: d["providers"].append(copy.deepcopy(d["providers"][0])),
        lambda d: d["records"][0].update(dataset_id="different"),
        lambda d: d["records"][0].update(provider="UNKNOWN"),
        lambda d: d["records"][0].update(title=""),
        lambda d: d["records"][0].update(kind="distribution"),
        lambda d: d["records"][0].update(kind=[]),
        lambda d: d["records"][0].update(guessed_topic="employment"),
        lambda d: d["records"][0].update(raw_resources=[]),
        lambda d: d["records"][0].update(raw_resources=["raw/missing.json"]),
        lambda d: d["records"][0].update(raw_resources=["../outside"]),
        lambda d: d["records"][0].update(raw_resources=["raw/catalogue.json"] * 2),
        lambda d: d["records"][0].update(references=[]),
        lambda d: d["records"][0]["references"][0].update(url="file:///tmp/evidence"),
        lambda d: d["records"][0]["references"][0].update(url="https://name:secret@example.org/"),
        lambda d: d["records"][0]["references"][0].update(resource_paths=["raw/missing.json"]),
        lambda d: d["records"][0].update(source_metadata={"bad": math.nan}),
        lambda d: d["records"][0].update(dimensions=["not_an_object"]),
        lambda d: d["records"][0].update(licence=42),
        lambda d: d["records"][0].update(last_checked_at="recently"),
        lambda d: d["records"][0].update(deleted="false"),
        lambda d: d["records"][1]["derived_evidence"][0].pop("asserted_by"),
        lambda d: d["providers"][0]["completeness"].update(discovered_count=1),
        lambda d: d["providers"][0]["completeness"].update(described_count=3),
        lambda d: d["providers"][0]["completeness"].update(discovered_count=True),
        lambda d: d["providers"][0]["completeness"].update(status=[]),
        lambda d: d["providers"][0]["completeness"].update(status="failed"),
        lambda d: d["providers"][0]["freshness"].update(captured_at=None),
    ],
)
def test_strict_core_rejects_malformed_metadata(mutate):
    document = synthetic_document()
    mutate(document)
    with pytest.raises(MetadataError):
        validate_document(document, ["raw/catalogue.json"])


@pytest.mark.parametrize("scope", [{"query": "households"}, {"dataset_ids": ["FAKE_001"]}])
def test_filtered_catalogue_cannot_claim_authoritative_inventory(scope):
    document = synthetic_document()
    document["providers"][0]["scope"] = scope
    document["providers"][0]["completeness"]["inventory_authoritative"] = True
    with pytest.raises(MetadataError, match="unfiltered inventory"):
        validate_document(document)


def test_partial_empty_failed_report_is_valid_without_deleting_records_from_other_snapshots():
    document = synthetic_document()
    document["records"] = []
    report = document["providers"][0]
    report["completeness"].update(
        status="failed",
        catalogue_complete=False,
        descriptions_complete=False,
        discovered_count=0,
        described_count=0,
        errors=[{"reason": "Synthetic interruption"}],
    )
    assert validate_document(document)["records"] == []


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"x":NaN}', b"[]", b"\xff"])
def test_json_decoder_rejects_duplicate_fields_nonfinite_and_invalid_utf8(raw):
    with pytest.raises(MetadataError):
        parse_json(raw)


def test_machine_schema_validates_v1_and_declares_all_strict_core_fields():
    from jsonschema import Draft202012Validator, FormatChecker

    from ssdu.metadata_contract import RECORD_FIELDS, contract_schema

    schema = contract_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(synthetic_document())
    assert set(schema["$defs"]["record"]["properties"]) == RECORD_FIELDS
    assert schema["properties"]["contract_version"] == {"const": CONTRACT_VERSION}
    incompatible = synthetic_document()
    incompatible["contract_version"] = "2.0.0"
    assert list(validator.iter_errors(incompatible))
    incompatible = synthetic_document()
    incompatible["records"][0]["unreviewed_core"] = "unsupported"
    assert list(validator.iter_errors(incompatible))
    incompatible = synthetic_document()
    incompatible["records"][0]["last_checked_at"] = "2026-10-15"
    assert list(validator.iter_errors(incompatible))
    # Callers cannot mutate a shared schema singleton.
    schema["properties"]["contract_version"]["const"] = "changed"
    assert contract_schema()["properties"]["contract_version"]["const"] == CONTRACT_VERSION


def test_legacy_snapshot_validation_and_schema_preserve_flexible_native_shapes():
    from jsonschema import Draft202012Validator

    from ssdu.metadata_contract import LEGACY_RECORD_FIELDS, contract_schema

    document = synthetic_document()
    document["contract_version"] = "1.0.0"
    document["records"][0]["dimensions"][0]["count"] = "native count not normalized"
    document["records"][0]["coverage"] = "native scope retained in v1.0"
    original = copy.deepcopy(document)
    assert validate_document(document) == original
    assert document == original
    schema = contract_schema("1.0.0")
    Draft202012Validator(schema).validate(document)
    assert set(schema["$defs"]["record"]["properties"]) == LEGACY_RECORD_FIELDS
    row = document["records"][0]
    assert validate_record(row, contract_version="1.0.0") == row
    assert record_version(row, contract_version="1.0.0")
    with pytest.raises(MetadataError):
        validate_record(row)


def discovery_document():
    document = synthetic_document()
    document["records"][0].update(
        kind="publication",
        publishers=[
            {"id": "invented-department", "name": "Invented Department", "role": "primary"}
        ],
        subjects=[{"id": "homes", "label": "Invented housing", "scheme": "provider"}],
        languages=["en-GB", "cy"],
        related=[
            {"relation": "isPartOf", "target_id": "nomis:FAKE_002", "title": "Invented series"}
        ],
        field_status={
            "subjects": {"status": "mapped"},
            "coverage.spatial": {
                "status": "not_provided",
                "reason": "Inspected provider field is absent.",
            },
            "measures": {"status": "not_requested"},
        },
        coverage={"temporal": {"scope": "provider_declared", "start": "2020", "end": "2024"}},
        distributions=[
            {
                "id": "download",
                "url": "https://example.org/invented.csv",
                "supported": False,
                "bytes": 0,
            }
        ],
    )
    return document


def test_current_contract_retains_typed_discovery_fields_without_inventing_facts():
    from jsonschema import Draft202012Validator, FormatChecker

    from ssdu.metadata_contract import contract_schema

    document = discovery_document()
    assert validate_document(document) == document
    Draft202012Validator(contract_schema(), format_checker=FormatChecker()).validate(document)
    document["contract_version"] = "1.0.0"
    with pytest.raises(MetadataError):
        validate_document(document)


@pytest.mark.parametrize(
    "fields",
    [
        {"publishers": [{}]},
        {"publishers": [{"name": "Invented", "url": "file:///local"}]},
        {"publishers": [{"name": "Invented", "url": "https://user:pass@example.org"}]},
        {"publishers": [{"name": "Invented", "guessed": True}]},
        {"subjects": [{"scheme": "unknown"}]},
        {"subjects": ["housing"]},
        {"languages": "en"},
        {"languages": [None]},
        {"related": [{"target_id": "other:row"}]},
        {"related": [{"relation": "isPartOf"}]},
        {"dimensions": [{"id": 3}]},
        {"dimensions": [{"count": "three"}]},
        {"dimensions": [{"values": "not an array"}]},
        {"measures": [{"unit": 4}]},
        {"distributions": [{"bytes": True}]},
        {"distributions": [{"supported": "yes"}]},
        {"distributions": [{"url": "../file.csv"}]},
        {"coverage": "UK"},
        {"coverage": {"spatial": "UK"}},
        {"coverage": {"temporal": {"start": 2020}}},
        {"coverage": {"spatial": {"areas": ["UK"]}}},
        {"coverage": {"spatial": {"areas": [{"id": "UK", "count": "many"}]}}},
        {"field_status": {"made_up": {"status": "mapped"}}},
        {"field_status": {"description": {"status": "inferred"}}},
        {"field_status": {"description": {"status": []}}},
        {"field_status": {"description": {"status": "unmapped", "reason": 4}}},
    ],
)
def test_typed_discovery_contract_and_schema_reject_malformed_fields(fields):
    from jsonschema import Draft202012Validator

    from ssdu.metadata_contract import contract_schema

    document = discovery_document()
    document["records"][0].update(fields)
    with pytest.raises(MetadataError):
        validate_document(document)
    assert list(Draft202012Validator(contract_schema()).iter_errors(document))


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "complete", "catalogue_complete": False},
        {"checked_dataset_count": 3},
        {"checked_dataset_count": True},
        {"codes_complete": "false"},
        {"inventory_authoritative": True, "status": "partial"},
    ],
)
def test_completeness_counters_and_booleans_are_consistent(fields):
    document = synthetic_document()
    document["providers"][0]["completeness"].update(fields)
    with pytest.raises(MetadataError):
        validate_document(document)


def test_explicit_dataset_scope_can_complete_without_claiming_full_catalogue():
    document = synthetic_document()
    report = document["providers"][0]
    report["scope"] = {"dataset_ids": ["FAKE_001", "FAKE_002"]}
    report["completeness"].update(catalogue_complete=False, status="complete")
    assert validate_document(document)["providers"][0]["completeness"]["status"] == "complete"


def test_unknown_provider_report_cannot_back_a_record_from_another_provider():
    document = synthetic_document()
    document["providers"][0]["provider"] = "scotland"
    with pytest.raises(MetadataError, match="lacks a provider report"):
        validate_document(document)


def test_quarantined_rows_can_retain_code_capture_counts_without_dataset_assertions():
    document = synthetic_document()
    document["records"] = []
    report = document["providers"][0]
    report["completeness"].update(
        status="partial",
        catalogue_complete=False,
        descriptions_complete=False,
        discovered_count=0,
        described_count=0,
        code_pages=2,
        quarantine_count=2,
    )
    result = validate_document(document, ["raw/catalogue.json"])
    assert result["providers"][0]["completeness"]["code_pages"] == 2
    assert result["records"] == []
