"""Invented JSON-stat metadata; no captured provider content or requests."""

import copy

import httpx
import pytest

from ssdu.adapters import nisra
from ssdu.http import HttpClient
from ssdu.models import RetrievalError


def cube():
    return {
        "class": "dataset",
        "version": "2.0",
        "id": ["STATISTIC", "PERIOD", "AREA"],
        "size": [2, 2, 1],
        "label": "Invented coverage table",
        "updated": "2025-02-10T09:00:00Z",
        "note": [
            "<p>Invented methods &amp; definitions.</p><script>hide()</script>",
            "<p>Second paragraph.</p>",
        ],
        "role": {"metric": ["STATISTIC"], "time": ["PERIOD"], "geo": ["AREA"]},
        "dimension": {
            "STATISTIC": {
                "label": "Measure",
                "category": {
                    "index": {"B": 1, "A": 0},
                    "label": {"A": "Invented number", "B": "Invented ratio"},
                    "unit": {"A": {"label": "Objects", "decimals": 0, "position": "end"}},
                },
            },
            "PERIOD": {
                "label": "Invented period",
                "category": {
                    "index": ["Z", "A"],
                    "label": {"Z": "Last listed", "A": "First named"},
                },
            },
            "AREA": {
                "label": "Invented geography",
                "category": {
                    "index": ["ISLAND"],
                    "label": {"ISLAND": "Invented island"},
                },
            },
        },
        "extension": {
            "matrix": "SYNTH_MAP",
            "copyright": {"name": "Invented owner", "href": "https://example.org/terms"},
            "contact": {"name": "Invented contact", "email": "test@example.org"},
        },
    }


def describe(document):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == (f"{nisra.BASE}/PxStat.Data.Cube_API.ReadMetadata/SYNTH_MAP/en")
        return httpx.Response(200, json=document)

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        result = nisra.describe_dataset("SYNTH_MAP", client)
    assert len(calls) == 1
    return result


def evidence(result, field):
    return {
        entry["source_path"]: entry["method"]
        for entry in result["metadata_mapping"]["evidence"]
        if entry["field"] == field
    }


def test_maps_prose_measure_roles_units_categories_and_copyright_with_one_request():
    document = cube()
    original = copy.deepcopy(document)
    result = describe(document)
    assert result["description"] == "Invented methods & definitions.\n\nSecond paragraph."
    assert result["measures"] == [
        {
            "id": "A",
            "label": "Invented number",
            "unit": document["dimension"]["STATISTIC"]["category"]["unit"]["A"],
        },
        {"id": "B", "label": "Invented ratio"},
    ]
    assert result["coverage"] == {
        "temporal": {
            "dimensions": [{"id": "PERIOD", "label": "Invented period"}],
            "periods": [{"id": "Z", "label": "Last listed"}, {"id": "A", "label": "First named"}],
            "scope": "dimension_categories",
        },
        "spatial": {
            "dimensions": [{"id": "AREA", "label": "Invented geography"}],
            "areas": [{"id": "ISLAND", "label": "Invented island"}],
            "scope": "dimension_categories",
        },
    }
    assert result["licence"] == "https://example.org/terms"
    assert result["access"] == {"metadata_url": result["source_url"], "data_access": "not_asserted"}
    assert result["metadata_mapping"]["version"] == "1.1.0"
    assert evidence(result, "description") == {
        "/note/0": "provider_assertion",
        "/note/1": "provider_assertion",
    }
    assert evidence(result, "measures") == {
        "/role/metric": "provider_dimension_role",
        "/dimension/STATISTIC/category/index": "provider_assertion",
        "/dimension/STATISTIC/category/label": "provider_assertion",
        "/dimension/STATISTIC/category/unit/A": "provider_assertion",
    }
    assert evidence(result, "licence") == {"/extension/copyright/href": "provider_assertion"}
    assert evidence(result, "access") == {"@response.url": "connector_endpoint"}
    assert evidence(result, "coverage.temporal")["/role/time"] == "provider_dimension_role"
    assert evidence(result, "coverage.spatial")["/role/geo"] == "provider_dimension_role"
    assert document == original


def test_missing_roles_notes_and_terms_stay_unknown_despite_suggestive_names():
    document = cube()
    document.pop("note")
    document.pop("role")
    document["extension"]["copyright"] = {"name": "Invented licensing authority"}
    result = describe(document)
    assert result["description"] is None
    assert result["measures"] == []
    assert result["coverage"] == {"spatial": None, "temporal": None}
    assert result["licence"] is None
    assert result["metadata_mapping"]["evidence"] == [
        {
            "field": "access",
            "source_path": "@response.url",
            "method": "connector_endpoint",
        }
    ]


@pytest.mark.parametrize(
    "href",
    [
        None,
        "",
        "javascript:alert(1)",
        "http://example.org/terms",
        "https://test:secret@example.org/terms",
        "https://example.org/a b",
        "https://example.org:123/terms",
    ],
)
def test_unsafe_or_missing_copyright_link_is_not_promoted_to_terms(href):
    document = cube()
    document["extension"]["copyright"]["href"] = href
    result = describe(document)
    assert result["licence"] is None
    assert evidence(result, "licence") == {}


def test_notes_accept_readable_native_language_and_skip_ambiguous_or_hidden_values():
    document = cube()
    document["note"] = [
        None,
        {"en": "Invented alternate"},
        "<style>hidden</style>",
        "<p>Données inventées.</p>",
    ]
    result = describe(document)
    assert result["description"] == "Données inventées."
    assert evidence(result, "description") == {"/note/3": "provider_assertion"}
    document["note"] = {"en": "Invented alternate"}
    assert describe(document)["description"] is None


def test_native_pointer_escaping_missing_labels_and_no_inferred_period_bounds():
    document = cube()
    document["id"][0] = "M/~"
    document["dimension"]["M/~"] = document["dimension"].pop("STATISTIC")
    document["role"]["metric"] = ["M/~"]
    category = document["dimension"]["M/~"]["category"]
    category["index"] = ["A/~", "B"]
    category.pop("label")
    category["unit"] = {"A/~": {"label": "Invented units"}}
    document["role"].pop("geo")
    result = describe(document)
    assert result["measures"] == [
        {"id": "A/~", "label": "A/~", "unit": {"label": "Invented units"}},
        {"id": "B", "label": "B"},
    ]
    assert "/dimension/M~1~0/category/unit/A~1~0" in evidence(result, "measures")
    assert result["coverage"]["spatial"] is None
    assert "start" not in result["coverage"]["temporal"]
    assert "end" not in result["coverage"]["temporal"]


def test_conflicting_roles_keep_existing_jsonstat_validation():
    document = cube()
    document["role"]["geo"] = ["PERIOD"]
    with pytest.raises(RetrievalError, match="overlapping roles"):
        describe(document)
