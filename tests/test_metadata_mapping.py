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
