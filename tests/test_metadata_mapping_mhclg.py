"""Independently invented GOV.UK Content API metadata; no captured provider content."""

import hashlib
import json

import httpx
import pytest

from ssdu.adapters import mhclg
from ssdu.http import HttpClient


def publication():
    return {
        "base_path": "/government/statistics/invented-metadata-example",
        "title": "Invented totals for Farland, 1970 to 2090",
        "description": "<p>Provider <b>summary</b> &amp; notes.</p><script>hidden()</script>",
        "first_published_at": "2025-03-01T00:00:00Z",
        "public_updated_at": "2026-04-01T00:00:00Z",
        "links": {
            "organisations": [{"slug": mhclg.ORGANISATIONS[0]}],
            "available_translations": [{"locale": "cy", "api_url": "https://example.invalid"}],
        },
        "details": {
            "body": "<p>Longer provider publication prose.</p>",
            "licence": "https://example.invalid/terms/invented",
            "national_applicability": {
                "nearland": {"applicable": True, "label": "Nearland"},
                "farland": {"applicable": False, "label": "Farland"},
            },
            "attachments": [
                {
                    "id": "invented-table",
                    "title": "Farland counts, 1970 to 2090",
                    "url": "https://assets.publishing.service.gov.uk/media/invented/table.csv",
                    "content_type": "text/csv",
                    "accessible": False,
                }
            ],
        },
    }


def describe(document):
    calls = []
    body = json.dumps(document).encode()

    def respond(request):
        calls.append(str(request.url))
        assert request.url.path == "/api/content" + document["base_path"]
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with HttpClient(transport=httpx.MockTransport(respond), allowed_hosts=mhclg.HOSTS) as client:
        result = mhclg.describe_dataset("invented-metadata-example", client)
    assert calls == [mhclg.BASE + "/api/content" + document["base_path"]]
    assert result["release"]["metadata_sha256"] == hashlib.sha256(body).hexdigest()
    return result


def evidence(result, field):
    return {
        (item["source_path"], item["method"])
        for item in result["metadata_mapping"]["evidence"]
        if item["field"] == field
    }


def test_maps_prose_licence_and_explicit_publication_applicability_without_download():
    result = describe(publication())
    assert result["description"] == "Provider summary & notes."
    assert result["licence"] == "https://example.invalid/terms/invented"
    spatial = result["coverage"]["spatial"]
    assert spatial["areas"] == [{"id": "nearland", "label": "Nearland"}]
    assert spatial["scope"] == "provider_declared"
    assert spatial["basis"] == "publication_applicability"
    assert "does not establish the geographic extent of observations" in spatial["note"]
    assert "temporal" not in result["coverage"]
    assert result["measures"] == result["dimensions"] == []
    assert result["access"] == {
        "metadata_url": mhclg.BASE + "/api/content/government/statistics/invented-metadata-example",
        "data_access": "not_asserted",
    }
    assert evidence(result, "description") == {("/description", "provider_assertion")}
    assert evidence(result, "licence") == {("/details/licence", "provider_assertion")}
    assert evidence(result, "coverage.spatial") == {
        ("/details/national_applicability/nearland/applicable", "provider_assertion"),
        ("/details/national_applicability/nearland/label", "provider_assertion"),
    }
    assert evidence(result, "access") == {("@response.url", "connector_endpoint")}
    assert result["metadata_mapping"]["version"] == "1.1.0"
    assert "body" not in result and "raw" not in result


def test_missing_assertions_stay_unknown_despite_title_dates_and_attachment_accessibility():
    document = publication()
    document.pop("description")
    for key in ("body", "licence", "national_applicability"):
        document["details"].pop(key)
    result = describe(document)
    assert result["description"] is None
    assert result["coverage"] is None
    assert result["measures"] == []
    assert result["licence"] == mhclg.UNKNOWN_LICENCE
    assert result["access"]["data_access"] == "not_asserted"
    assert "conditions" not in result["access"]
    assert {item["field"] for item in result["metadata_mapping"]["evidence"]} == {"access"}


def test_body_is_readable_description_fallback_with_original_field_evidence():
    document = publication()
    document["description"] = "<script>hidden</script>"
    document["details"]["body"] = "<style>hidden</style><p>First.</p><p>Second.</p>"
    result = describe(document)
    assert result["description"] == "First.\n\nSecond."
    assert evidence(result, "description") == {("/details/body", "provider_assertion")}


def test_non_english_provider_description_is_preserved_without_translation_requests():
    document = publication()
    document["locale"] = "cy"
    document["description"] = "<p>Disgrifiad dychmygol.</p>"
    result = describe(document)
    assert result["description"] == "Disgrifiad dychmygol."
    assert evidence(result, "description") == {("/description", "provider_assertion")}


@pytest.mark.parametrize("licence", [None, " ", [], {}, 1, "<script>hidden</script>"])
def test_malformed_or_unreadable_licence_keeps_existing_unknown_contract(licence):
    document = publication()
    document["details"]["licence"] = licence
    result = describe(document)
    assert result["licence"] == mhclg.UNKNOWN_LICENCE
    assert not evidence(result, "licence")


@pytest.mark.parametrize("applicability", [None, [], "Nearland", True])
def test_malformed_applicability_is_not_geographic_coverage(applicability):
    document = publication()
    document["details"]["national_applicability"] = applicability
    result = describe(document)
    assert result["coverage"] is None
    assert not evidence(result, "coverage.spatial")


def test_only_true_applicability_with_readable_labels_is_mapped_and_paths_are_escaped():
    document = publication()
    document["details"]["national_applicability"] = {
        "false": {"applicable": False, "label": "Farland"},
        "integer": {"applicable": 1, "label": "Farland"},
        "string": {"applicable": "true", "label": "Farland"},
        "unknown": {"label": "Farland"},
        "missing_label": {"applicable": True},
        "hidden": {"applicable": True, "label": "<script>Farland</script>"},
        "bad": "Nearland",
        "island~/one": {"applicable": True, "label": "<b>Island One</b>"},
    }
    result = describe(document)
    assert result["coverage"]["spatial"]["areas"] == [{"id": "island~/one", "label": "Island One"}]
    assert evidence(result, "coverage.spatial") == {
        ("/details/national_applicability/island~0~1one/applicable", "provider_assertion"),
        ("/details/national_applicability/island~0~1one/label", "provider_assertion"),
    }
