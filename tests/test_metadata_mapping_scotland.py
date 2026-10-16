"""Independently invented RDF metadata; no captured publisher content."""

import copy

import httpx
import pytest

from ssdu.adapters.scotland import (
    AREA,
    BASE,
    DCT,
    ENDPOINT,
    PERIOD,
    QB,
    RDFS,
    UNIT,
    describe_dataset,
    metadata_query,
)
from ssdu.http import HttpClient
from ssdu.models import RetrievalError

DATASET = "ssdu-Invented-Cohort"
MEASURE = "https://example.invalid/metadata/measure/total"
CATEGORY = "https://example.invalid/metadata/dimension/group"


def term(value, kind="literal", language=None):
    result = {"type": kind, "value": value}
    if language:
        result["xml:lang"] = language
    return result


def statement(predicate, value, kind="literal", language=None):
    return {
        "kind": term("dataset"),
        "property": term(predicate, "uri"),
        "value": term(value, kind, language),
    }


def component(prop, role, label=None):
    row = {
        "kind": term(QB + role),
        "property": term(prop, "uri"),
        "value": term(prop, "uri"),
    }
    if label:
        row["label"] = term(label)
    return row


def payload(*extra, geographic=True, temporal=True):
    rows = [
        statement(QB + "structure", "https://example.invalid/metadata/structure/one", "uri"),
        statement(DCT + "title", "Invented coastal cohort 1880–2099"),
        statement(DCT + "issued", "2025-07-03T00:00:00Z"),
        component(CATEGORY, "dimension", "Group"),
        component(MEASURE, "measure", "Invented total"),
        component(UNIT, "attribute", "Unit"),
        *([component(AREA, "dimension", "Area code")] if geographic else []),
        *([component(PERIOD, "dimension", "Period code")] if temporal else []),
        *extra,
    ]
    return {
        "head": {"vars": ["kind", "property", "value", "label", "attachment", "required"]},
        "results": {"bindings": rows},
    }


def describe(source):
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url.copy_with(query=None)) == ENDPOINT
        assert request.url.params["query"] == metadata_query(DATASET)
        return httpx.Response(200, json=source)

    with HttpClient(
        transport=httpx.MockTransport(handler), allowed_hosts=frozenset({"statistics.gov.scot"})
    ) as client:
        result = describe_dataset(DATASET, client)
        assert len(client.resources) == 1
    assert len(calls) == 1
    return result


def test_maps_prose_declared_measures_roles_and_rights_from_one_response():
    source = payload(
        statement(DCT + "description", "<p>An invented cohort.</p><p>Provider methods.</p>"),
        statement(DCT + "license", "https://example.invalid/metadata/terms", "uri"),
        statement(DCT + "accessRights", "Request permission for restricted tables."),
    )
    result = describe(source)
    assert result["dataset_id"] == DATASET
    assert result["source_url"] == BASE + DATASET
    assert result["description"] == "An invented cohort.\n\nProvider methods."
    assert result["measures"] == [{"id": MEASURE, "label": "Invented total"}]
    assert result["coverage"] == {
        "spatial": {
            "dimensions": [{"id": AREA, "label": "Area code"}],
            "scope": "declared_dimension",
        },
        "temporal": {
            "dimensions": [{"id": PERIOD, "label": "Period code"}],
            "scope": "declared_dimension",
        },
    }
    assert result["licence"] == "https://example.invalid/metadata/terms"
    assert result["access"] == {
        "metadata_url": ENDPOINT,
        "data_access": "not_asserted",
        "conditions": "Request permission for restricted tables.",
    }
    assert result["source_metadata"]["bindings"] == source["results"]["bindings"]
    evidence = result["metadata_mapping"]
    assert evidence["version"] == "1.1.0"
    assert {
        "field": "description",
        "source_path": f"<{BASE}{DATASET}>/<{DCT}description>",
        "method": "provider_assertion",
    } in evidence["evidence"]
    assert any(
        item["field"] == "measures" and item["source_path"].endswith(f"/<{QB}measure>")
        for item in evidence["evidence"]
    )
    assert all(
        item["method"] == "declared_dimension"
        for item in evidence["evidence"]
        if item["field"].startswith("coverage.")
    )


def test_unknown_optional_fields_and_observation_unit_remain_unknown():
    result = describe(payload(geographic=False, temporal=False))
    assert result["description"] is None
    assert result["licence"] is None
    assert result["coverage"] is None
    assert "unit" not in result["measures"][0]
    assert result["access"] == {"metadata_url": ENDPOINT, "data_access": "not_asserted"}
    assert {item["field"] for item in result["metadata_mapping"]["evidence"]} == {
        "measures",
        "access",
    }


def test_native_dimension_roles_do_not_follow_ambiguous_labels_or_dates():
    source = payload(geographic=False, temporal=False)
    source["results"]["bindings"] += [
        component(
            "https://example.invalid/metadata/dimension/other", "dimension", "Reference Area"
        ),
        component(
            "https://example.invalid/metadata/dimension/date", "dimension", "Reference Period"
        ),
    ]
    result = describe(source)
    assert result["coverage"] is None
    assert len(result["measures"]) == 1


def test_description_prefers_english_then_untagged_and_keeps_unique_other_language():
    for language, expected in (("en-GB", "Invented summary."), (None, "Invented summary.")):
        result = describe(
            payload(
                statement(DCT + "description", "Résumé inventé.", language="fr"),
                statement(DCT + "description", expected, language=language),
            )
        )
        assert result["description"] == expected
    assert (
        describe(payload(statement(DCT + "description", "Résumé inventé.", language="fr")))[
            "description"
        ]
        == "Résumé inventé."
    )


def test_comment_fallback_is_native_prose_and_uri_is_not_description():
    result = describe(
        payload(
            statement(DCT + "description", "https://example.invalid/not-prose", "uri"),
            statement(RDFS + "comment", "Provider comment for an invented population."),
        )
    )
    assert result["description"] == "Provider comment for an invented population."
    assert any(
        item["source_path"].endswith(f"/<{RDFS}comment>")
        for item in result["metadata_mapping"]["evidence"]
    )


def test_conflicting_optional_assertions_are_not_arbitrarily_selected():
    result = describe(
        payload(
            statement(DCT + "description", "First summary.", language="en"),
            statement(DCT + "description", "Conflicting summary.", language="en"),
            statement(DCT + "license", "First terms."),
            statement(DCT + "license", "Different terms."),
            statement(DCT + "accessRights", "Approval required."),
            statement(DCT + "accessRights", "Registration required."),
        )
    )
    assert result["description"] is None and result["licence"] is None
    assert "conditions" not in result["access"]
    assert not any(
        item["field"] in {"description", "licence"}
        for item in result["metadata_mapping"]["evidence"]
    )


def test_safe_text_drops_executable_markup_without_licence_assumptions():
    result = describe(
        payload(
            statement(DCT + "description", "<script>hidden()</script><b>Visible summary.</b>"),
            statement(DCT + "license", "javascript:invented()", "uri"),
            statement(DCT + "accessRights", "<style>hidden</style>Explicit conditions."),
        )
    )
    assert result["description"] == "Visible summary."
    assert result["licence"] is None
    assert result["access"]["conditions"] == "Explicit conditions."


def test_mapping_is_invariant_to_repeated_statements_and_row_order():
    source = payload(statement(DCT + "description", "Invented description."))
    reordered = copy.deepcopy(source)
    reordered["results"]["bindings"] += copy.deepcopy(source["results"]["bindings"])
    reordered["results"]["bindings"].reverse()
    first, second = describe(source), describe(reordered)
    for field in ("description", "measures", "coverage", "licence", "access", "metadata_mapping"):
        assert first[field] == second[field]


def test_existing_component_ambiguity_is_still_rejected():
    source = payload(component(MEASURE, "measure", "A conflicting label"))
    with pytest.raises(RetrievalError, match="Ambiguous Scotland component"):
        describe(source)
