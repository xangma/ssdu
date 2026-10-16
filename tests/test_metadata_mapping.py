"""Shared pure mapping behavior and field-level retained source evidence."""

import copy

import pytest

from ssdu.metadata_mapping import MAPPING_VERSION, bind_mapping, mapping_metadata, plain_text
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
