"""Synthetic StatsWales revision assertions; no captured provider payloads."""

import copy
import socket

import httpx
import pytest

from ssdu.adapters import statswales
from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient
from ssdu.metadata_mapping import MAPPING_VERSION

DATASET = "00000000-0000-4000-8000-000000000041"


def describe(metadata, dimensions=None):
    calls = []
    native = [] if dimensions is None else dimensions

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert dict(request.url.params) == {"lang": "en-gb"}
        if request.url.path == f"/v2/{DATASET}":
            return httpx.Response(200, json=metadata)
        assert request.url.path == f"/v2/{DATASET}/filters"
        return httpx.Response(200, json=native)

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        result = statswales.describe_dataset(DATASET, client)
    assert len(calls) == 2
    return result


def revision(**fields):
    return {"id": DATASET, "published_revision": fields}


def test_maps_summary_and_declared_dates_with_native_evidence():
    metadata = revision(
        id="synthetic-revision",
        metadata=[
            {"language": "cy-GB", "title": "Gweithdai", "summary": "Crynodeb arall."},
            {
                "language": "en-GB",
                "title": "Workshop observations",
                "summary": (
                    "<p>Survey of workshops.</p><script>hidden()</script>"
                    "<p>Includes repairs &amp; maintenance.</p>"
                ),
            },
        ],
        coverage_start_date="2004-04-01",
        coverage_end_date="2006-03-31",
    )
    original = copy.deepcopy(metadata)
    result = describe(metadata)
    assert result["description"] == "Survey of workshops.\n\nIncludes repairs & maintenance."
    assert result["coverage"] == {
        "temporal": {
            "start": "2004-04-01",
            "end": "2006-03-31",
            "scope": "provider_declared",
        }
    }
    assert result["access"]["metadata_url"] == f"{statswales.BASE}/{DATASET}?lang=en-gb"
    assert result["access"]["data_access"] == "not_asserted"
    assert result["metadata_mapping"]["version"] == MAPPING_VERSION
    assert {
        (item["field"], item["source_path"], item["method"])
        for item in result["metadata_mapping"]["evidence"]
    } >= {
        ("description", "/published_revision/metadata/1/summary", "provider_assertion"),
        ("coverage.temporal", "/published_revision/coverage_start_date", "provider_assertion"),
        ("coverage.temporal", "/published_revision/coverage_end_date", "provider_assertion"),
        ("access", "@response.url", "connector_endpoint"),
    }
    assert result["source_metadata"] == original
    assert metadata == original


def test_missing_assertions_do_not_use_title_publication_or_dimension_labels():
    metadata = revision(
        metadata=[{"language": "en-GB", "title": "Annual data for example regions 2040"}],
        publish_at="2041-03-01T09:00:00Z",
        update_frequency={"date": {"year": "2042"}, "update_type": "update"},
    )
    dimensions = [
        {
            "columnName": label,
            "factTableColumn": key,
            "values": [
                {
                    "reference": "parent",
                    "description": "Example grouping",
                    "count": "0",
                    "children": [
                        {"reference": "child", "description": "Example member", "count": "5"}
                    ],
                }
            ],
        }
        for key, label in (
            ("category", "Data description"),
            ("unrecognized_place_axis", "Area name"),
            ("YearID", "Financial year"),
        )
    ]
    result = describe(metadata, dimensions)
    assert result["description"] is None
    assert result["measures"] == []
    assert result["coverage"] is None
    assert result["licence"] is None
    assert [item["source_metadata"] for item in result["dimensions"]] == dimensions
    assert [item["value_count"] for item in result["dimensions"]] == [2, 2, 2]
    assert not any(
        item["field"]
        in {"description", "measures", "coverage.spatial", "coverage.temporal", "licence"}
        for item in result["metadata_mapping"]["evidence"]
    )
    assert result["field_status"]["measures"]["status"] == "unmapped"
    assert result["field_status"]["coverage.spatial"]["status"] == "unmapped"
    assert result["field_status"]["licence"]["status"] == "not_provided"


def test_welsh_summary_fallback_keeps_its_original_native_location():
    result = describe(
        revision(
            metadata=[
                {"language": "cy-GB", "summary": "<p>Disgrifiad enghreifftiol o weithdai.</p>"},
                {"language": "en-GB", "title": "Workshop observations", "summary": " "},
            ]
        )
    )
    assert result["description"] == "Disgrifiad enghreifftiol o weithdai."
    assert {
        "field": "description",
        "source_path": "/published_revision/metadata/0/summary",
        "method": "provider_assertion",
    } in result["metadata_mapping"]["evidence"]


def test_conflicting_preferred_language_summaries_remain_unknown():
    result = describe(
        revision(
            metadata=[
                {"language": "en-GB", "summary": "First assertion."},
                {"language": "en-gb", "summary": "Second assertion."},
                {"language": "cy-GB", "summary": "Crynodeb arall."},
            ]
        )
    )
    assert result["description"] is None
    assert not any(
        item["field"] == "description" for item in result["metadata_mapping"]["evidence"]
    )


@pytest.mark.parametrize("translated", [None, {}, [None, {"language": "en-GB", "summary": {}}]])
def test_malformed_optional_summary_is_not_stringified(translated):
    result = describe(revision(metadata=translated))
    assert result["description"] is None
    assert result["title"] == DATASET


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2007-02-01", None, {"start": "2007-02-01"}),
        (None, "2007-02-28", {"end": "2007-02-28"}),
        ("2007-02-30", "2007-02-28", {"end": "2007-02-28"}),
        ("2008-01-01", "2007-01-01", None),
        ("2007", "2008-02-30", None),
        ({"date": "2007-01-01"}, 2008, None),
    ],
)
def test_only_valid_explicit_nonconflicting_date_bounds_are_mapped(start, end, expected):
    result = describe(revision(coverage_start_date=start, coverage_end_date=end))
    assert result["coverage"] == (
        {"temporal": {**expected, "scope": "provider_declared"}} if expected else None
    )
    paths = {
        item["source_path"]
        for item in result["metadata_mapping"]["evidence"]
        if item["field"] == "coverage.temporal"
    }
    assert paths == {f"/published_revision/coverage_{bound}_date" for bound in (expected or {})}


def test_normalizes_explicit_publishers_topics_languages_and_relationships():
    metadata = revision(
        metadata=[
            {"language": "cy-GB", "title": "Gweithdai"},
            {"language": "en-GB", "title": "Workshops"},
        ],
        providers=[
            {
                "language": "cy-gb",
                "provider_id": "source-office",
                "provider_name": "Swyddfa",
                "source_id": "survey-1",
                "source_name": "Arolwg",
            },
            {
                "language": "en-gb",
                "provider_id": "source-office",
                "provider_name": "Invented Office",
                "source_id": "survey-1",
                "source_name": "Invented survey",
            },
        ],
        topics=[
            {"id": 42, "name_en": "Invented workshops", "name_cy": "Gweithdai", "path": "2.42"}
        ],
        related_links=[{"url": "https://example.org/methods", "label_en": "Survey methods"}],
        previous_revision_id="revision-one",
    )
    metadata["publisher"] = {
        "organisation": {"id": "org-1", "name": "Invented Administration"},
        "group": {"id": "team-1", "name": "Invented Statistics"},
    }
    metadata["replaced_by"] = {"dataset_id": "replacement", "dataset_title": "New workshops"}
    original = copy.deepcopy(metadata)
    result = describe(metadata)
    assert result["publishers"] == [
        {"id": "org-1", "name": "Invented Administration", "role": "publisher"},
        {"id": "team-1", "name": "Invented Statistics", "role": "publishing_group"},
        {"id": "source-office", "name": "Invented Office", "role": "data_provider"},
    ]
    assert result["subjects"] == [
        {"id": "42", "label": "Invented workshops", "scheme": "StatsWales topics"}
    ]
    assert result["languages"] == ["cy-gb", "en-gb"]
    assert result["related"] == [
        {"relation": "source", "target_id": "survey-1", "title": "Invented survey"},
        {"relation": "related", "url": "https://example.org/methods", "title": "Survey methods"},
        {"relation": "previous_revision", "target_id": "revision-one"},
        {"relation": "is_replaced_by", "target_id": "replacement", "title": "New workshops"},
    ]
    for field in ("publishers", "subjects", "languages", "related"):
        assert result["field_status"][field]["status"] == "mapped"
        assert any(row["field"] == field for row in result["metadata_mapping"]["evidence"])
    assert metadata == original == result["source_metadata"]


def test_dimension_categories_are_normalized_without_guessing_roles_or_units():
    native = [
        {
            "factTableColumn": "MeasureID",
            "columnName": "Area and measure",
            "values": [
                {
                    "reference": "all",
                    "description": "Total",
                    "children": [{"reference": "part", "description": "Part"}],
                }
            ],
        }
    ]
    result = describe(revision(), native)
    assert result["dimensions"][0]["values"] == [
        {"id": "all", "label": "Total"},
        {"id": "part", "label": "Part"},
    ]
    assert result["field_status"]["dimensions"]["status"] == "mapped"
    assert result["measures"] == []
    assert result["coverage"] is None
    assert result["dimensions"][0]["source_metadata"] == native[0]


def test_invalid_optional_assertions_remain_native_and_have_visible_mapping_gap():
    metadata = revision(
        topics=[None],
        related_links=[{"url": "javascript:invented()"}],
        providers=[{"provider_id": 12, "provider_name": {"unexpected": "object"}}],
    )
    metadata["publisher"] = "unexpected shape"
    result = describe(metadata)
    assert result["publishers"] == result["subjects"] == result["related"] == []
    for field in ("publishers", "subjects", "related"):
        assert result["field_status"][field]["status"] == "unmapped"
    assert result["source_metadata"] == metadata


def native_dimension(identity, label, *members):
    return {
        "factTableColumn": identity,
        "columnName": label,
        "values": [{"reference": identity, "description": label} for identity, label in members],
    }


def test_controlled_filter_roles_project_measures_and_native_geography_categories():
    native = [
        native_dimension("DataDescription", "Data description", ("m1", "Invented workshop count")),
        native_dimension("area_code", "Area", ("a1", "Invented region")),
        native_dimension("MeasureID", "Area and measure", ("x1", "Ambiguous category")),
        native_dimension("unexpected_axis", "Area", ("W99999999", "Unverified place")),
    ]
    original = copy.deepcopy(native)
    result = describe(revision(), native)
    assert result["measures"] == [
        {
            "id": "m1",
            "label": "Invented workshop count",
            "dimension": "DataDescription",
            "scheme": "StatsWales filter categories",
        }
    ]
    spatial = result["coverage"]["spatial"]
    assert spatial["areas"] == [
        {
            "id": "a1",
            "label": "Invented region",
            "dimension": "area_code",
            "scheme": "StatsWales filter categories",
        }
    ]
    assert spatial["scope"] == "dimension_categories"
    assert spatial["basis"] == "statswales_filter_vocabulary_v1"
    assert [item.get("role") for item in result["dimensions"]] == ["metric", "geo", None, None]
    assert "unit" not in result["measures"][0]
    for field in ("measures", "coverage.spatial"):
        assert result["field_status"][field]["status"] == "mapped"
        assert {
            "field": field,
            "source_path": "@filters",
            "method": "provider_dimension_role",
        } in result["metadata_mapping"]["evidence"]
    assert native == original


def test_revision_dates_take_precedence_and_preserve_conflicting_catalogue_assertions():
    metadata = revision(coverage_start_date="2030-01-01", coverage_end_date="2031-12-31")
    metadata.update(start_date="2020-01-01", end_date="2032-12-31")
    result = describe(metadata)
    temporal = result["coverage"]["temporal"]
    assert temporal["start"] == "2030-01-01" and temporal["end"] == "2031-12-31"
    assert temporal["basis"] == "published_revision"
    assert temporal["alternatives"] == [
        {"start": "2020-01-01", "end": "2032-12-31", "basis": "dataset_catalogue_dates"}
    ]
    assert temporal["assertions_disagree"] is True
    assert result["field_status"]["coverage.temporal"]["status"] == "ambiguous"
    assert {
        item["source_path"]
        for item in result["metadata_mapping"]["evidence"]
        if item["field"] == "coverage.temporal"
    } == {
        "/published_revision/coverage_start_date",
        "/published_revision/coverage_end_date",
        "/start_date",
        "/end_date",
    }


def test_complete_catalogue_fallback_is_explicitly_scoped_and_never_fills_revision_bounds():
    metadata = revision()
    metadata.update(start_date="2030-01-01", end_date="2031-12-31")
    result = describe(metadata)
    assert result["coverage"]["temporal"] == {
        "start": "2030-01-01",
        "end": "2031-12-31",
        "scope": "provider_declared",
        "basis": "dataset_catalogue_dates",
        "revision_coverage": "not_provided",
    }
    metadata["published_revision"]["coverage_start_date"] = "2030-06-01"
    result = describe(metadata)
    assert result["coverage"]["temporal"]["start"] == "2030-06-01"
    assert "end" not in result["coverage"]["temporal"]


def test_matching_partial_assertions_do_not_claim_a_date_disagreement():
    metadata = revision(coverage_start_date="2030-01-01")
    metadata.update(start_date="2030-01-01", end_date="2031-12-31")
    result = describe(metadata)
    temporal = result["coverage"]["temporal"]
    assert temporal["start"] == "2030-01-01" and "end" not in temporal
    assert "assertions_disagree" not in temporal
    assert temporal["alternatives"] == [
        {"start": "2030-01-01", "end": "2031-12-31", "basis": "dataset_catalogue_dates"}
    ]
    assert result["field_status"]["coverage.temporal"]["status"] == "mapped"


@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "2030-01-01"},
        {"start_date": "2032-01-01", "end_date": "2030-01-01"},
        {"start_date": "2030-02-30", "end_date": "2031-01-01"},
    ],
)
def test_invalid_or_partial_catalogue_dates_do_not_become_revision_coverage(dates):
    result = describe({**revision(), **dates})
    assert result["coverage"] is None
    assert result["field_status"]["coverage.temporal"]["status"] == "unmapped"


def test_bad_revision_dates_are_not_silently_replaced_by_catalogue_dates():
    metadata = revision(coverage_start_date="2032-01-01", coverage_end_date="2030-01-01")
    metadata.update(start_date="2030-01-01", end_date="2031-12-31")
    result = describe(metadata)
    assert result["coverage"] is None
    assert result["field_status"]["coverage.temporal"]["status"] == "unmapped"


def test_archive_replacement_and_modification_are_distinct_from_publication_and_retrievability():
    metadata = revision(updated_at="2030-03-01T08:00:00Z", publish_at="2030-03-02T09:00:00Z")
    metadata.update(
        archived_at="2031-04-01T09:00:00Z",
        replaced_by={"dataset_id": "replacement", "dataset_title": "Invented replacement"},
    )
    result = describe(metadata)
    assert result["release"]["updated"] == "2030-03-01T08:00:00Z"
    assert result["release"]["publish_at"] == "2030-03-02T09:00:00Z"
    assert result["release"]["lifecycle"] == {
        "status": "archived",
        "archived_at": "2031-04-01T09:00:00Z",
        "source_path": "/archived_at",
        "replacement": metadata["replaced_by"],
        "replacement_source_path": "/replaced_by",
    }
    assert result["capabilities"]["retrieval"] is True
    assert result["access"]["retrieval"]["supported"] is True
    assert result["access"]["retrieval"]["status"] == "supported"
    assert result["access"]["retrieval"]["constraints"] == {
        "selection_method": "server_side_native_filters",
        "published_revision_only": True,
        "bounded_query": True,
    }
    assert result["access"]["data_access"] == "not_asserted"
    del metadata["archived_at"]
    assert describe(metadata)["release"]["lifecycle"]["status"] == "replaced"
    del metadata["replaced_by"]
    assert describe(metadata)["release"]["lifecycle"]["status"] == "not_asserted"


def test_enriched_descriptor_replays_identically_from_cache_without_network(tmp_path, monkeypatch):
    metadata = revision(
        coverage_start_date="2030-01-01",
        coverage_end_date="2031-12-31",
        updated_at="2032-01-01T08:30:00Z",
    )
    native = [
        native_dimension("Measure", "Data description", ("m1", "Invented repairs")),
        native_dimension("AreaCode", "Area", ("a1", "Invented town")),
    ]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            200, json=native if request.url.path.endswith("/filters") else metadata
        )

    with (
        HttpClient(transport=httpx.MockTransport(handler), retries=0) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        first = statswales.describe_dataset(DATASET, client)
    assert len(calls) == 2

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline metadata replay attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(HttpClient, "get", forbidden)
    with CachedClient(tmp_path, offline=True) as client:
        assert statswales.describe_dataset(DATASET, client) == first
    assert len(calls) == 2
