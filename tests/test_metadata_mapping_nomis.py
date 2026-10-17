"""Invented SDMX definitions; no captured provider payloads or network traffic."""

import copy

import httpx
import pytest

from ssdu.adapters import nomis
from ssdu.http import HttpClient
from ssdu.metadata_mapping import MAPPING_VERSION

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
    assert result["measures"] == [
        {"id": "OBS_VALUE", "label": "OBS_VALUE", "role": "value_column", "unit": "visits"}
    ]
    assert result["coverage"] == {
        "spatial": {
            "dimensions": [
                {"id": "geography", "label": "GEOGRAPHY", "codelist": "CL_INVENTED_AREA"}
            ],
            "scope": "declared_dimension",
        },
        "temporal": {
            "dimensions": [{"id": "time", "label": "TIME", "codelist": "CL_INVENTED_TIME"}],
            "scope": "declared_dimension",
        },
    }
    assert result["licence"] is None
    assert result["access"]["metadata_url"] == f"{nomis.BASE}/{DATASET}/def.sdmx.json"
    assert result["access"]["data_access"] == "not_asserted"
    assert result["access"]["retrieval"]["supported"] is True
    mapping = result["metadata_mapping"]
    assert mapping["version"] == MAPPING_VERSION
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
        "dimensions",
        "languages",
        "related",
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
    assert {item["field"] for item in result["metadata_mapping"]["evidence"]} == {
        "access",
        "dimensions",
    }
    assert result["field_status"]["description"]["status"] == "not_provided"


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
    measure = result["measures"][0]
    assert measure["role"] == "value_column"
    assert "unit" not in measure
    assert measure["dataset_unit_hints"] == [
        {"label": "visits", "scope": "dataset_annotation", "applies_to_all_series": False},
        {"label": "percent", "scope": "dataset_annotation", "applies_to_all_series": False},
    ]
    assert all(set(value) == {"dimensions", "scope"} for value in result["coverage"].values())
    assert not any(
        item["field"].startswith("coverage.") and "annotationtext" in item["source_path"]
        for item in result["metadata_mapping"]["evidence"]
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
    assert result["measures"] == [
        {
            "id": "OBS_VALUE",
            "label": "OBS_VALUE",
            "role": "value_column",
            "dataset_unit_hint": {
                "label": "visits",
                "scope": "dataset_annotation",
                "applies_to_all_series": False,
            },
        }
    ]
    assert any(
        item["field"] == "measures" and "annotationtext" in item["source_path"]
        for item in result["metadata_mapping"]["evidence"]
    )
    assert result["source_metadata"] == document


def test_nomis_annotation_prose_classifications_languages_and_codelists_are_normalized():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family.pop("description")
    family["agencyid"] = "INVENTED_AGENCY"
    family["name"]["lang"] = "en"
    family["annotations"]["annotation"] += [
        {"annotationtitle": "SubDescription", "annotationtext": "Invented population base."},
        {"annotationtitle": "MetadataTitle0", "annotationtext": "Invented methods"},
        {
            "annotationtitle": "MetadataText0",
            "annotationtext": "<p>Invented notes &amp; caveats.</p>",
        },
        {"annotationtitle": "Keywords", "annotationtext": "libraries, visits, libraries"},
        {"annotationtitle": "contenttype/sources", "annotationtext": "invented_survey"},
        {"annotationtitle": "contenttype/geoglevel", "annotationtext": "invented_level"},
    ]
    result = describe(document)
    assert (
        result["description"]
        == "Invented population base.\n\nInvented methods\nInvented notes & caveats."
    )
    assert result["publishers"] == [{"id": "INVENTED_AGENCY", "role": "metadata_agency"}]
    assert result["languages"] == ["en"]
    assert result["subjects"] == [
        {"label": "libraries", "scheme": "nomis:Keywords"},
        {"label": "visits", "scheme": "nomis:Keywords"},
        {"id": "invented_survey", "scheme": "nomis:contenttype/sources"},
        {"id": "invented_level", "scheme": "nomis:contenttype/geoglevel"},
    ]
    assert result["related"] == [
        {"relation": "codelist", "target_id": "CL_INVENTED_AREA"},
        {"relation": "codelist", "target_id": "CL_INVENTED_CATEGORY"},
        {"relation": "codelist", "target_id": "CL_INVENTED_TIME"},
    ]
    assert result["dimensions"][-1]["id"] == "time"
    assert result["field_status"]["description"]["status"] == "mapped"
    assert result["field_status"]["licence"]["status"] == "not_provided"
    assert result["field_status"]["distributions"]["status"] == "not_requested"
    assert result["source_metadata"] == document


def test_nomis_conflicting_description_has_explicit_status_without_inventing_text():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family["description"] = [
        {"lang": "en", "value": "Account A"},
        {"lang": "en", "value": "Account B"},
    ]
    result = describe(document)
    assert result["description"] is None
    assert result["field_status"]["description"]["status"] == "ambiguous"
    assert result["field_status"]["publishers"]["status"] == "not_provided"
    assert result["source_metadata"] == document


def test_nomis_revision_marker_is_exposed_without_inventing_timezone_or_extent():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family["annotations"]["annotation"].append(
        {"annotationtitle": "LastUpdated", "annotationtext": "2088-03-04 09:30:00"}
    )
    result = describe(document)
    assert result["release"]["updated"] == "2088-03-04 09:30:00"
    assert result["release"]["provider_revision"]["LastUpdated"] == "2088-03-04 09:30:00"
    assert all("start" not in item for item in result["coverage"].values())
    assert result["capabilities"]["retrieval"] is True
    assert result["access"]["data_access"] == "not_asserted"


def test_nomis_conflicting_updated_annotations_are_not_selected_arbitrarily():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family["annotations"]["annotation"] += [
        {"annotationtitle": "LastUpdated", "annotationtext": "2088-03-04 09:30:00"},
        {"annotationtitle": "LastUpdated", "annotationtext": "2088-03-05 09:30:00"},
    ]
    result = describe(document)
    assert "updated" not in result["release"]
    assert result["source_metadata"] == document


def test_nomis_varying_measure_units_have_scoped_hints_and_codelist_references():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family["components"]["dimension"].append(
        {"conceptref": "MEASURES", "codelist": "CL_INVENTED_MEASURES"}
    )
    result = describe(document)
    assert result["measures"] == [
        {
            "id": "OBS_VALUE",
            "label": "OBS_VALUE",
            "role": "value_column",
            "dataset_unit_hint": {
                "label": "visits",
                "scope": "dataset_annotation",
                "applies_to_all_series": False,
            },
        }
    ]
    assert result["coverage"]["spatial"]["dimensions"][0]["codelist"] == "CL_INVENTED_AREA"
    assert result["coverage"]["temporal"]["dimensions"][0]["codelist"] == "CL_INVENTED_TIME"
    assert result["coverage"]["spatial"]["scope"] == "declared_dimension"
    assert "not requested" in result["field_status"]["coverage.spatial"]["reason"]


def test_nomis_unreadable_native_metadata_is_unmapped_rather_than_absent():
    document = definition()
    family = document["structure"]["keyfamilies"]["keyfamily"][0]
    family["agencyid"] = {"unexpected": "object"}
    family["description"] = {"value": {"unsupported": "shape"}}
    family["name"]["lang"] = {"unsupported": "shape"}
    family["annotations"]["annotation"].append(
        {"annotationtitle": "Keywords", "annotationtext": {"unsupported": "shape"}}
    )
    result = describe(document)
    for field in ("description", "publishers", "subjects", "languages"):
        assert result["field_status"][field]["status"] == "unmapped"
    assert result["source_metadata"] == document


@pytest.mark.parametrize("selector", ["time", "date"])
def test_declared_time_dimension_supports_time_and_date_code_requests(selector):
    document = definition()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == f"/api/v01/dataset/{DATASET}/def.sdmx.json":
            return httpx.Response(200, json=document)
        assert request.url.path == f"/api/v01/dataset/{DATASET}/time.def.sdmx.json"
        return httpx.Response(
            200,
            json={
                "structure": {
                    "codelists": {
                        "codelist": {
                            "code": [
                                {
                                    "value": "invented-period",
                                    "description": {"value": "Invented period"},
                                }
                            ]
                        }
                    }
                }
            },
        )

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        descriptor = nomis.describe_dataset(DATASET, client)
        result = nomis.list_dimension_values(DATASET, selector, client)
    assert descriptor["dimensions"][-1]["id"] == "time"
    assert result["items"] == [
        {
            "value": "invented-period",
            "label": "Invented period",
            "source_metadata": {
                "value": "invented-period",
                "description": {"value": "Invented period"},
            },
        }
    ]
    assert result["total"] == 1
    assert calls == [
        f"/api/v01/dataset/{DATASET}/def.sdmx.json",
        f"/api/v01/dataset/{DATASET}/def.sdmx.json",
        f"/api/v01/dataset/{DATASET}/time.def.sdmx.json",
    ]
    assert descriptor["coverage"]["temporal"]["scope"] == "declared_dimension"
    assert "start" not in descriptor["coverage"]["temporal"]
