"""Shared pure mapping behavior and field-level retained source evidence."""

import copy

import pytest

from ssdu.metadata_mapping import (
    MAPPING_FIELDS,
    MAPPING_VERSION,
    bind_mapping,
    mapping_metadata,
    normalized_metadata_fields,
    plain_text,
    provider_timestamp,
)
from ssdu.providers import get_provider


def test_provider_prose_is_readable_text_without_active_markup():
    assert plain_text(None) is None
    assert plain_text(["unknown prose representation"]) is None
    assert (
        plain_text(
            "<p>Invented &amp; readable.</p><script>do not retain</script><p>[b]Note[/b]</p>"
        )
        == "Invented & readable.\n\nNote"
    )
    assert plain_text("<style>hidden</style> <template>hidden</template>") is None
    assert (
        plain_text("Less than 5 < 10 and more than 2 > 1") == "Less than 5 < 10 and more than 2 > 1"
    )


def test_incomplete_entities_and_provider_links_do_not_erase_prose():
    assert plain_text("hello &world") == "hello &world"
    assert plain_text("Invented notes &tab=charts).") == "Invented notes &tab=charts)."
    assert plain_text("Research &amp") == "Research &"
    assert plain_text("[url=https://example.invalid/notes?a=1&b=2][b]Methods[/b][/url]") == (
        "Methods (https://example.invalid/notes?a=1&b=2)"
    )
    assert plain_text("[url]https://example.invalid/[/url]") == "https://example.invalid/"
    assert plain_text("<script>hidden &unfinished") is None


@pytest.mark.parametrize(
    "raw,value,precision,timezone",
    [
        ("2026-03-11T9:30:00.000Z", "2026-03-11T09:30:00.000Z", "fractional_second", "explicit"),
        ("2026-01-02 09:30:00", "2026-01-02T09:30:00", "second", "unspecified"),
        ("2026-01-02T09:30+01:00", "2026-01-02T09:30+01:00", "minute", "explicit"),
        ("2026-01-02", "2026-01-02", "day", "not_applicable"),
        ("2026-01", "2026-01", "month", "not_applicable"),
        ("2026", "2026", "year", "not_applicable"),
    ],
)
def test_provider_clock_retains_raw_precision_and_timezone_uncertainty(
    raw, value, precision, timezone
):
    assert provider_timestamp(raw) == {
        "raw": raw,
        "value": value,
        "precision": precision,
        "timezone_status": timezone,
        "status": "normalized",
    }


@pytest.mark.parametrize("raw", ["unreleased", "2026-02-30", "2026-01-02T99:30:00Z"])
def test_opaque_or_invalid_clock_is_retained_without_a_false_instant(raw):
    assert provider_timestamp(raw)["raw"] == raw
    assert provider_timestamp(raw)["value"] is None
    assert provider_timestamp(raw)["status"] == "unparsed"


def test_publication_date_does_not_claim_provider_modification():
    from ssdu.metadata_sync import MetadataSync

    assert MetadataSync._modified({"release": {"published": "2026-01-02"}}) is None
    base = {"release": {"updated": "2026-03-11T9:30:00.000Z", "published": "2026-01-02"}}
    assert MetadataSync._modified(base) == "2026-03-11T09:30:00.000Z"
    assert [item["kind"] for item in MetadataSync._timestamps(base)] == ["modified", "published"]


def test_retrieval_capability_is_separate_from_access_rights_and_never_inferred():
    base = {
        "access": {
            "metadata_url": "https://example.invalid/metadata",
            "data_access": "not_asserted",
        },
        "capabilities": {
            "retrieval": False,
            "retrieval_reason": "No supported distribution.",
            "publisher_eligible": True,
        },
    }
    result = normalized_metadata_fields(base)
    assert result["access"]["data_access"] == "not_asserted"
    assert result["access"]["retrieval"] == {
        "supported": False,
        "status": "unsupported",
        "reason": "No supported distribution.",
        "constraints": {},
    }
    assert "retrieval" not in base["access"]
    unknown = normalized_metadata_fields({"capabilities": {"dimension_values": True}})
    assert unknown["access"]["retrieval"]["supported"] is None
    assert unknown["access"]["retrieval"]["status"] == "unknown"
    assert "access" not in normalized_metadata_fields({})


def test_field_evidence_is_deterministic_and_binds_verified_description_resources():
    item = {"field": "description", "source_path": "/summary", "method": "provider_assertion"}
    endpoint = {"field": "access", "source_path": "@response.url", "method": "connector_endpoint"}
    descriptor = {"metadata_mapping": mapping_metadata([item, endpoint, item])}
    original = copy.deepcopy(descriptor)
    result = bind_mapping(descriptor, ["raw/second.bin", "raw/first.bin", "raw/first.bin"])
    assert result["version"] == MAPPING_VERSION
    assert len(result["evidence"]) == 2
    assert all(
        x["resource_paths"] == ["raw/first.bin", "raw/second.bin"] for x in result["evidence"]
    )
    assert descriptor == original
    assert bind_mapping(None, []) == {"version": MAPPING_VERSION, "evidence": []}
    with pytest.raises(ValueError, match="response evidence"):
        bind_mapping(descriptor, [])


@pytest.mark.parametrize(
    "change",
    [
        {"field": "invented_assertion"},
        {"method": "guessed_from_title"},
        {"source_path": ""},
        {"source_path": None},
    ],
)
def test_unsupported_mapping_claims_are_rejected(change):
    item = {"field": "licence", "source_path": "/rights", "method": "provider_assertion"}
    with pytest.raises(ValueError):
        mapping_metadata([{**item, **change}])


@pytest.mark.parametrize("provider", ["nomis", "statswales", "mhclg", "nisra", "scotland"])
def test_shared_mapping_interpretation_is_part_of_provider_fingerprint(provider):
    assert "metadata_mapping.py" in {p.name for p in get_provider(provider).implementation_paths()}


def test_normalized_additions_distinguish_unmapped_from_inspected_missing_fields():
    base = {
        "description": "Invented summary",
        "dimensions": [{"id": "area"}],
        "publishers": [{"name": "Invented publisher"}],
        "subjects": [{"label": "Invented subject"}],
        "languages": ["en"],
        "metadata_mapping": mapping_metadata(
            [
                {"field": "description", "source_path": "/summary", "method": "provider_assertion"},
                {
                    "field": "publishers",
                    "source_path": "/publisher",
                    "method": "provider_assertion",
                },
            ]
        ),
        "field_status": {
            "licence": {
                "status": "not_provided",
                "reason": "Inspected provider rights field is absent.",
            },
            "measures": {"status": "not_requested"},
            "coverage.spatial": {"status": "ambiguous", "reason": "Two conflicting extents."},
        },
    }
    original = copy.deepcopy(base)
    result = normalized_metadata_fields(base)
    assert result["publishers"] == base["publishers"]
    assert result["related"] == []
    assert set(result["field_status"]) == MAPPING_FIELDS
    assert result["field_status"]["description"] == {"status": "mapped"}
    assert result["field_status"]["publishers"] == {"status": "mapped"}
    assert result["field_status"]["dimensions"]["status"] == "unmapped"
    assert result["field_status"]["subjects"]["status"] == "unmapped"
    assert result["field_status"]["coverage.temporal"]["status"] == "unmapped"
    for field in base["field_status"]:
        assert result["field_status"][field] == base["field_status"][field]
    result["publishers"][0]["name"] = "Changed"
    result["field_status"]["licence"]["reason"] = "Changed"
    assert base == original


def test_evidence_without_a_normalized_value_does_not_claim_mapping_or_provider_omission():
    result = normalized_metadata_fields(
        {
            "metadata_mapping": mapping_metadata(
                [
                    {"field": "licence", "source_path": "/rights", "method": "provider_assertion"},
                ]
            )
        }
    )
    assert all(item["status"] == "unmapped" for item in result["field_status"].values())
    assert all(result[key] == [] for key in ("publishers", "subjects", "languages", "related"))


@pytest.mark.parametrize(
    "field_status",
    [
        [],
        {"invented": {"status": "mapped"}},
        {"licence": {}},
        {"licence": {"status": []}},
        {"licence": {"status": "guessed"}},
        {"licence": {"status": "unmapped", "reason": None}},
    ],
)
def test_normalization_rejects_unsupported_adapter_statuses(field_status):
    with pytest.raises(ValueError):
        normalized_metadata_fields({"field_status": field_status})
