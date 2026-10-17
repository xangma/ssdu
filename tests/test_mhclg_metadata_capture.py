"""Invented publications test metadata scope independently of retrieval eligibility."""

import copy
from unittest.mock import patch

import httpx
import pytest

from ssdu.adapters import mhclg
from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError
from ssdu.rate_limit import RateLimiter

MHCLG = mhclg.ORGANISATIONS[0]
OTHER = "invented-culture-department"
PATH = "/government/statistics/invented-associated-statistics"


def organisation(slug, title):
    return {"slug": slug, "title": title, "base_path": "/government/organisations/" + slug}


def publication():
    return {
        "base_path": PATH,
        "title": "Invented associated statistics, 1980 to 2090",
        "document_type": "official_statistics",
        "description": "<p>Invented provider summary.</p>",
        "locale": "en",
        "first_published_at": "2025-02-01T00:00:00Z",
        "public_updated_at": "2026-03-01T00:00:00Z",
        "links": {
            "primary_publishing_organisation": [organisation(OTHER, "Invented Culture Department")],
            "organisations": [
                organisation(OTHER, "Invented Culture Department"),
                organisation(MHCLG, "Invented MHCLG label"),
            ],
            "taxons": [{"content_id": "invented-topic", "title": "Invented social activity"}],
            "available_translations": [{"locale": "cy", "api_url": "https://example.invalid"}],
            "document_collections": [
                {"base_path": "/government/collections/invented-series", "title": "Invented series"}
            ],
        },
        "details": {
            "licence": "https://example.invalid/invented-licence",
            "national_applicability": {"invented": {"applicable": True, "label": "Invented land"}},
            "attachments": [
                {
                    "id": "invented-csv",
                    "title": "Invented file",
                    "url": "https://assets.publishing.service.gov.uk/media/invented/data.csv",
                    "content_type": "text/csv",
                    "file_size": 25,
                },
                {
                    "id": "invented-pdf",
                    "url": "https://assets.publishing.service.gov.uk/media/invented/report.pdf",
                    "content_type": "application/pdf",
                },
            ],
            "invented_unmapped_property": {"kept": [1, "two"]},
        },
    }


def client_for(document, tmp_path, calls):
    def respond(request):
        calls.append(str(request.url))
        assert request.url.path == "/api/content" + PATH
        return httpx.Response(200, json=document)

    return HttpClient(
        transport=httpx.MockTransport(respond),
        allowed_hosts=mhclg.HOSTS,
        rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
    )


def test_associated_metadata_retains_actual_primary_and_unsupported_distributions(tmp_path):
    document, calls = publication(), []
    with client_for(document, tmp_path, calls) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    assert calls == [mhclg.BASE + "/api/content" + PATH]
    assert result["kind"] == "publication"
    assert result["publisher"] == "Invented Culture Department"
    assert result["publishers"] == [
        {
            "id": OTHER,
            "name": "Invented Culture Department",
            "url": mhclg.BASE + "/government/organisations/" + OTHER,
            "role": "primary",
        },
        {
            "id": MHCLG,
            "name": "Invented MHCLG label",
            "url": mhclg.BASE + "/government/organisations/" + MHCLG,
            "role": "associated",
        },
    ]
    assert result["capabilities"]["retrieval"] is False
    assert result["capabilities"]["formats"] == []
    assert "outside the download policy" in result["capabilities"]["retrieval_reason"]
    assert {item["id"] for item in result["distributions"]} == {"invented-csv", "invented-pdf"}
    assert not any(item["supported"] for item in result["distributions"])
    assert result["source_metadata"]["document"] == document
    assert result["source_metadata"]["document"]["details"]["invented_unmapped_property"] == {
        "kept": [1, "two"]
    }


def test_metadata_mapping_exposes_subjects_languages_associations_and_honest_coverage(tmp_path):
    with client_for(publication(), tmp_path, []) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    assert result["subjects"] == [
        {"id": "invented-topic", "label": "Invented social activity", "scheme": "govuk:taxons"}
    ]
    assert result["languages"] == ["en", "cy"]
    assert any(item["relation"] == "associated_organisation" for item in result["related"])
    assert {
        "relation": "is_part_of",
        "target_id": "/government/collections/invented-series",
        "url": mhclg.BASE + "/government/collections/invented-series",
        "title": "Invented series",
    } in result["related"]
    assert result["coverage"]["spatial"]["basis"] == "publication_applicability"
    assert "temporal" not in result["coverage"]
    for field in ("subjects", "languages", "publishers", "related", "licence", "distributions"):
        assert result["field_status"][field]["status"] == "mapped"
        assert any(entry["field"] == field for entry in result["metadata_mapping"]["evidence"])
    assert result["field_status"]["dimensions"]["status"] == "not_requested"
    assert result["field_status"]["measures"]["status"] == "not_requested"
    assert result["field_status"]["coverage.temporal"]["status"] == "not_provided"
    assert result["access"]["data_access"] == "not_asserted"


@pytest.mark.parametrize("operation", ["describe_dataset", "fetch"])
def test_metadata_scope_does_not_widen_strict_download_gate(operation, tmp_path):
    calls = []
    with (
        client_for(publication(), tmp_path, calls) as client,
        pytest.raises(RetrievalError, match="not published by MHCLG"),
    ):
        if operation == "fetch":
            mhclg.fetch(Request("mhclg", "invented-associated-statistics"), client)
        else:
            mhclg.describe_dataset("invented-associated-statistics", client)
    assert len(calls) == 1


def test_unassociated_metadata_remains_outside_scope(tmp_path):
    document = publication()
    document["links"]["organisations"] = [organisation(OTHER, "Invented Culture Department")]
    with (
        client_for(document, tmp_path, []) as client,
        pytest.raises(RetrievalError, match="not published by MHCLG"),
    ):
        mhclg.describe_metadata("invented-associated-statistics", client)


@pytest.mark.parametrize("document_type", ["guidance", "consultation", "statistical_announcement"])
def test_metadata_capture_keeps_statistical_content_type_scope(document_type, tmp_path):
    document = publication()
    document["document_type"] = document_type
    with (
        client_for(document, tmp_path, []) as client,
        pytest.raises(RetrievalError, match="statistical content types"),
    ):
        mhclg.describe_metadata("invented-associated-statistics", client)


def test_historical_catalogue_organisation_is_in_scope_without_widening_content_types(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        assert set(request.url.params.get_list("filter_organisations")) == mhclg.ACCEPTED_PUBLISHERS
        assert request.url.params.get_list("filter_content_store_document_type") == list(
            mhclg.DOCUMENT_TYPES
        )
        return httpx.Response(200, json={"results": [], "total": 0})

    with HttpClient(
        transport=httpx.MockTransport(respond),
        rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
    ) as client:
        assert mhclg.list_datasets("", client)["items"] == []
    assert len(calls) == 1
    assert "ministry-of-housing-communities-and-local-government-2018-2021" in mhclg.ORGANISATIONS


def test_cache_replays_full_metadata_without_network_or_attachment_download(tmp_path):
    document, calls = publication(), []
    document["details"]["body"] = "<p>Invented body-only topic for discovery.</p>"
    original = copy.deepcopy(document)
    with client_for(document, tmp_path, calls) as transport:
        with CachedClient(tmp_path, client=transport) as cache:
            captured = mhclg.describe_metadata("invented-associated-statistics", cache)
        with CachedClient(tmp_path, client=transport) as cache:
            assert mhclg.describe_metadata("invented-associated-statistics", cache) == captured
    assert len(calls) == 1
    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected offline HTTP")),
        CachedClient(tmp_path, offline=True) as cache,
    ):
        assert mhclg.describe_metadata("invented-associated-statistics", cache) == captured
    assert captured["source_metadata"]["document"] == original
    assert "body-only topic" in captured["description"]
    assert captured["summary"] == "Invented provider summary."


def test_invalid_native_values_are_distinguished_from_missing_assertions(tmp_path):
    document = publication()
    document.pop("description")
    document["locale"] = "invalid language"
    document["links"].pop("available_translations")
    document["links"]["taxons"] = [{"unknown_key": "not a mapped label"}]
    document["details"]["licence"] = {"unknown_key": "retained"}
    with client_for(document, tmp_path, []) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    assert result["field_status"]["description"]["status"] == "not_provided"
    for field in ("subjects", "languages", "licence"):
        assert result["field_status"][field]["status"] == "unmapped"


def test_publisher_eligibility_does_not_promise_retrieval_for_pdf_only_publication(tmp_path):
    document = publication()
    document["links"]["primary_publishing_organisation"] = [organisation(MHCLG, "Invented MHCLG")]
    document["details"]["attachments"] = [document["details"]["attachments"][1]]
    with client_for(document, tmp_path, []) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    assert result["capabilities"]["publisher_eligible"] is True
    assert result["capabilities"]["retrieval"] is False
    assert result["capabilities"]["formats"] == []
    assert result["capabilities"]["supported_distribution_count"] == 0
    assert "No supported CSV or CSVW" in result["capabilities"]["retrieval_reason"]
    with pytest.raises(RetrievalError, match="missing or ambiguous"):
        mhclg.validate_selection(Request("mhclg", "invented-associated-statistics"), result)


def test_supported_resource_flags_preserve_selection_and_trusted_host_requirements(tmp_path):
    document = publication()
    document["links"]["primary_publishing_organisation"] = [organisation(MHCLG, "Invented MHCLG")]
    document["details"]["attachments"].append(
        {"id": "untrusted", "url": "https://example.invalid/data.csv", "content_type": "text/csv"}
    )
    with client_for(document, tmp_path, []) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    capabilities = result["capabilities"]
    assert capabilities["publisher_eligible"] is True
    assert capabilities["retrieval"] is True
    assert capabilities["formats"] == ["csv"]
    assert capabilities["supported_distribution_count"] == 1
    assert "distribution" in capabilities["retrieval_constraints"]
    assert (
        mhclg.validate_selection(Request("mhclg", "invented-associated-statistics"), result)["id"]
        == "invented-csv"
    )
    unsupported = {item["id"]: item for item in result["distributions"] if not item["supported"]}
    assert "format" in unsupported["invented-pdf"]["unsupported_reason"]
    assert "trusted" in unsupported["untrusted"]["unsupported_reason"]
    with pytest.raises(RetrievalError, match="unsupported format or untrusted URL"):
        mhclg.validate_selection(
            Request(
                "mhclg", "invented-associated-statistics", params={"distribution": "untrusted"}
            ),
            result,
        )


def test_native_html_and_external_links_do_not_become_path_formats(tmp_path):
    document = publication()
    document["details"]["attachments"] = [
        {"id": "html", "attachment_type": "html", "url": "/government/statistics/invented/report"},
        {
            "id": "external",
            "attachment_type": "external",
            "url": "https://example.invalid/reports/current",
        },
        {"id": "unknown", "url": "https://assets.publishing.service.gov.uk/no-extension"},
        {
            "id": "external-suffix",
            "attachment_type": "external",
            "url": "https://assets.publishing.service.gov.uk/media/invented/file.csv",
        },
    ]
    with client_for(document, tmp_path, []) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    assert {item["id"]: item["format"] for item in result["distributions"]} == {
        "html": "text/html",
        "external": "unknown",
        "unknown": "unknown",
        "external-suffix": "unknown",
    }
    assert all(item["content_type"] is None for item in result["distributions"])
    assert not any(item["supported"] for item in result["distributions"])


def test_original_publisher_and_children_are_retained_without_following_links(tmp_path):
    document, calls = publication(), []
    document["links"]["original_primary_publishing_organisation"] = [
        organisation(OTHER, "Invented original publisher")
    ]
    document["links"]["children"] = [
        {
            "content_id": "invented-child",
            "title": "Invented child",
            "base_path": "/government/statistics/invented-child",
        }
    ]
    with client_for(document, tmp_path, calls) as client:
        result = mhclg.describe_metadata("invented-associated-statistics", client)
    assert len(calls) == 1
    assert result["publication_type"] == "official_statistics"
    assert any(item["role"] == "original" and item["id"] == OTHER for item in result["publishers"])
    assert {
        "relation": "has_part",
        "target_id": "invented-child",
        "title": "Invented child",
        "url": mhclg.BASE + "/government/statistics/invented-child",
    } in result["related"]
    evidence = result["metadata_mapping"]["evidence"]
    assert any(
        item["source_path"] == "/links/children/0" and item["field"] == "related"
        for item in evidence
    )
    assert any(
        item["source_path"] == "/links/original_primary_publishing_organisation/0"
        and item["field"] == "publishers"
        for item in evidence
    )
