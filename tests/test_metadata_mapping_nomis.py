"""Invented SDMX definitions; no captured provider payloads or network traffic."""

import copy

import httpx
import pytest

from ssdu.adapters import nomis
from ssdu.http import HttpClient

DATASET = "NM_9876_9"


def definition():
    return {
        "structure": {
            "keyfamilies": {
                "keyfamily": [
                    {
                        "id": DATASET,
                        "name": {"value": "Invented library visits"},
                        "description": {
                            "lang": "en",
                            "value": "<p>Invented visits &amp; loans.</p><script>unsafe()</script>",
                        },
                        "components": {
                            "dimension": [
                                {"conceptref": "GEOGRAPHY", "codelist": "CL_INVENTED_AREA"},
                                {"conceptref": "CATEGORY", "codelist": "CL_INVENTED_CATEGORY"},
                            ],
                            "timedimension": {"conceptref": "TIME", "codelist": "CL_INVENTED_TIME"},
                            "primarymeasure": {"conceptref": "OBS_VALUE"},
                        },
                        "annotations": {
                            "annotation": [{"annotationtitle": "Units", "annotationtext": "visits"}]
                        },
                    }
                ]
            }
        }
    }


def describe(document):
    original = copy.deepcopy(document)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.path == f"/api/v01/dataset/{DATASET}/def.sdmx.json"
        return httpx.Response(200, json=document)

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        result = nomis.describe_dataset(DATASET, client)
    assert calls == [f"{nomis.BASE}/{DATASET}/def.sdmx.json"]
    assert document == original
    return result


@pytest.mark.parametrize("singleton", [False, True])
def test_nomis_declared_metadata_and_native_evidence(singleton):
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    prefix = "/structure/keyfamilies/keyfamily"
    if singleton:
        document["structure"]["keyfamilies"]["keyfamily"] = family
        family["annotations"]["annotation"] = family["annotations"]["annotation"][0]
        family["components"]["dimension"] = family["components"]["dimension"][0]
    else:
        prefix += "/0"
    result = describe(document)
    assert result["description"] == "Invented visits & loans."
    assert result["measures"] == [{"id": "OBS_VALUE", "label": "OBS_VALUE", "unit": "visits"}]
    assert result["coverage"] == {
        "spatial": {
            "dimensions": [{"id": "geography", "label": "GEOGRAPHY"}],
            "scope": "declared_dimension",
        },
        "temporal": {
            "dimensions": [{"id": "time", "label": "TIME"}],
            "scope": "declared_dimension",
        },
    }
    assert result["licence"] is None
    assert result["access"] == {
        "metadata_url": f"{nomis.BASE}/{DATASET}/def.sdmx.json",
        "data_access": "not_asserted",
    }
    mapping = result["metadata_mapping"]
    assert mapping["version"] == "1.1.0"
    assert {
        "field": "description",
        "source_path": prefix + "/description/value",
        "method": "provider_assertion",
    } in mapping["evidence"]
    assert {item["field"] for item in mapping["evidence"]} == {
        "description",
        "measures",
        "coverage.spatial",
        "coverage.temporal",
        "access",
    }
    for item in mapping["evidence"]:
        if item["method"] == "connector_endpoint":
            assert item["source_path"] == "@response.url"
            continue
        value = document
        for segment in item["source_path"].split("/")[1:]:
            value = value[int(segment)] if isinstance(value, list) else value[segment]
        assert value is not None


def test_nomis_unknown_metadata_does_not_derive_from_title_or_category():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family.pop("description")
    family.pop("annotations")
    family["name"]["value"] = "Invented national counts, 1700 to 2400"
    family["components"] = {"dimension": [{"conceptref": "CATEGORY"}, {"conceptref": "MEASURES"}]}
    result = describe(document)
    assert result["description"] is None
    assert result["measures"] == []
    assert result["coverage"] is None
    assert result["licence"] is None
    assert [item["field"] for item in result["metadata_mapping"]["evidence"]] == ["access"]


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            [{"lang": "fr", "value": "Texte inventé"}, {"lang": "en-GB", "value": "Invented text"}],
            "Invented text",
        ),
        ({"lang": "fr", "value": "Texte inventé"}, "Texte inventé"),
        (
            [{"lang": "en", "value": "First account"}, {"lang": "en", "value": "Other account"}],
            None,
        ),
        ({"lang": "en", "value": "<style>hidden</style><script>unsafe()</script>"}, None),
        ({"lang": "en", "value": {"unexpected": "object"}}, None),
        (None, None),
    ],
)
def test_nomis_description_language_ambiguity_and_unsafe_content(description, expected):
    document = definition()
    document["structure"]["keyfamilies"]["keyfamily"][0]["description"] = description
    result = describe(document)
    assert result["description"] == expected
    assert any(
        item["field"] == "description" for item in result["metadata_mapping"]["evidence"]
    ) == (expected is not None)


def test_nomis_conflicting_units_and_publication_dates_do_not_create_extents():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family["annotations"]["annotation"] += [
        {"annotationtitle": "Units", "annotationtext": "percent"},
        {"annotationtitle": "FirstReleased", "annotationtext": "2077-01-01"},
        {"annotationtitle": "LastUpdated", "annotationtext": "2088-12-31"},
    ]
    result = describe(document)
    assert result["measures"] == [{"id": "OBS_VALUE", "label": "OBS_VALUE"}]
    assert all(set(value) == {"dimensions", "scope"} for value in result["coverage"].values())
    assert not any(
        "annotationtext" in item["source_path"] for item in result["metadata_mapping"]["evidence"]
    )


@pytest.mark.parametrize("varying", ["measure_selector", "series_unit", "both"])
def test_nomis_dataset_units_are_not_universal_for_varying_series(varying):
    document = definition()
    components = document["structure"]["keyfamilies"]["keyfamily"][0]["components"]
    if varying in {"measure_selector", "both"}:
        components["dimension"].append(
            {"conceptref": "MEASURES", "codelist": "CL_INVENTED_MEASURE"}
        )
    if varying in {"series_unit", "both"}:
        components["attribute"] = [
            {"conceptref": "UNIT", "attachmentlevel": "Series", "codelist": "CL_INVENTED_UNIT"},
            {"conceptref": "UNIT_MULTIPLIER", "attachmentlevel": "Series"},
        ]
    result = describe(document)
    assert result["measures"] == [{"id": "OBS_VALUE", "label": "OBS_VALUE"}]
    assert not any(
        "annotationtext" in item["source_path"] for item in result["metadata_mapping"]["evidence"]
    )
    assert result["source_metadata"] == document
