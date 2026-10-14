import json
from uuid import UUID

import httpx
import pytest

from ssdu.harvest import METADATA_HOSTS, MetadataClient
from ssdu.http import HttpClient
from ssdu.literature_store import LiteratureStore
from ssdu.models import RetrievalError
from ssdu.provider_harvest import ProviderHarvestError, harvest_provider_metadata
from ssdu.rate_limit import RateLimiter

IDS = [f"{number:08d}-1234-4321-9876-123456789012" for number in range(1, 5)]
NOMIS = {
    "structure": {
        "keyfamilies": {
            "keyfamily": [
                {
                    "id": "NM_1_1",
                    "name": {"value": "Claimant count"},
                    "description": {"value": "A provider description of claimant counts"},
                    "version": 1.0,
                    "annotations": {
                        "annotation": [
                            {"annotationtitle": "LastUpdated", "annotationtext": "2026-09-15"}
                        ]
                    },
                    "components": {
                        "dimension": [{"conceptref": "GEOGRAPHY", "codelist": "CL_GEO"}],
                        "timedimension": {"conceptref": "TIME", "codelist": "CL_TIME"},
                    },
                }
            ]
        }
    }
}


def source_fixture(count=3):
    rows = [
        {"id": identity, "title": "Poverty dataset " + str(index)}
        for index, identity in enumerate(IDS[:count])
    ]
    return rows


def http_for(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        allowed_hosts=METADATA_HOSTS,
        rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
        retries=0,
    )


def run(tmp_path, handler, plan):
    with http_for(tmp_path, handler) as http:
        client = MetadataClient(tmp_path, client=http)
        result = harvest_provider_metadata(tmp_path, plan, client=client)
    return result, LiteratureStore(tmp_path).read(result["snapshot_id"])


def test_nomis_singleton_and_dimensions_are_metadata_only_and_referenced(tmp_path):
    calls = []
    document = json.loads(json.dumps(NOMIS))
    document["structure"]["keyfamilies"]["keyfamily"] = document["structure"]["keyfamilies"][
        "keyfamily"
    ][0]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=document)

    result, doc = run(tmp_path, handler, {"schema_version": 1, "nomis": {"enabled": True}})
    assert result["counts"]["datasets"] == 1
    assert result["coverage"]["nomis"]["dimension_declarations_present"] == 1
    descriptor = doc["catalogue_descriptors"][0]
    assert [column["name"] for column in descriptor["columns"]] == ["geography", "time"]
    assert descriptor["description"] == "A provider description of claimant counts"
    assert descriptor["release"]["provider_revision"]["LastUpdated"] == "2026-09-15"
    assert descriptor["source_metadata"]["dataset_definition_version"] == 1.0
    assert descriptor["source_metadata"]["resource_path"] == "nomis/catalogue.json"
    assert descriptor["source_metadata"]["location"] == "/structure/keyfamilies/keyfamily"
    assert doc["datasets"][0]["references"] == [
        {key: doc["sources"][0][key] for key in ("id", "title", "url")}
    ]
    assert len(calls) == 1 and calls[0].url.path.endswith("/def.sdmx.json")
    assert doc["papers"] == doc["relationships"] == []
    assert not descriptor["retrieval_supported"] and doc["datasets"][0]["version"] is None


def test_statswales_count_pagination_and_partial_budget(tmp_path):
    rows = source_fixture()
    calls = []

    def handler(request):
        calls.append(request)
        page = int(request.url.params["page_number"])
        return httpx.Response(
            200, json={"data": rows[(page - 1) * 2 : page * 2], "count": len(rows)}
        )

    plan = {"schema_version": 1, "statswales": {"enabled": True, "page_size": 2, "max_pages": 2}}
    result, doc = run(tmp_path, handler, plan)
    assert result["coverage"]["statswales"]["provider_total"] == 3
    assert result["coverage"]["statswales"]["source_inventory_complete"]
    assert not result["coverage"]["statswales"]["budget_truncated"]
    assert len(doc["datasets"]) == 3 and len(calls) == 2
    assert all(row["coverage"] == {} for row in doc["datasets"])
    plan["statswales"]["max_pages"] = 1
    with http_for(tmp_path, lambda _: pytest.fail("Cached page made a request")) as http:
        client = MetadataClient(tmp_path, client=http)
        result = harvest_provider_metadata(tmp_path, plan, client=client, offline=True)
    coverage = result["coverage"]["statswales"]
    assert not coverage["source_inventory_complete"] and coverage["budget_truncated"]
    assert coverage["missing_source_rows"] == 1 and result["attempts"] == 0


def test_statswales_whole_801_catalogue_uses_one_bounded_request(tmp_path):
    rows = [{"id": str(UUID(int=index + 1)), "title": f"Dataset {index}"} for index in range(801)]
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["page_size"] == "1000"
        assert request.url.params["page_number"] == "1"
        return httpx.Response(200, json={"data": rows, "count": 801})

    result, doc = run(tmp_path, handler, {"schema_version": 1, "statswales": {"enabled": True}})
    assert len(calls) == 1 and len(doc["datasets"]) == 801
    coverage = result["coverage"]["statswales"]
    assert coverage["provider_total"] == coverage["imported"] == coverage["source_rows"] == 801
    assert coverage["pages_captured"] == 1 and coverage["source_inventory_complete"]
    assert not coverage["budget_truncated"] and coverage["quarantined"] == 0


def test_failed_second_page_can_resume_cached_first_page_and_replay_offline(tmp_path):
    rows = source_fixture()
    calls = []
    fail_second = True

    def handler(request):
        calls.append(int(request.url.params["page_number"]))
        page = calls[-1]
        if page == 2 and fail_second:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"data": rows[(page - 1) * 2 : page * 2], "count": len(rows)}
        )

    plan = {"schema_version": 1, "statswales": {"enabled": True, "page_size": 2}}
    with http_for(tmp_path, handler) as http, pytest.raises(RetrievalError):
        harvest_provider_metadata(tmp_path, plan, client=MetadataClient(tmp_path, client=http))
    assert not LiteratureStore(tmp_path).list()
    fail_second = False
    result, _ = run(tmp_path, handler, plan)
    assert calls == [1, 2, 2] and result["cache_hits"] == 1
    with http_for(tmp_path, lambda _: pytest.fail("Offline replay made a request")) as http:
        replay = harvest_provider_metadata(
            tmp_path, plan, client=MetadataClient(tmp_path, client=http), offline=True
        )
    assert replay["snapshot_id"] == result["snapshot_id"] and replay["attempts"] == 0
    assert replay["cache_hits"] == 2


@pytest.mark.parametrize("kind", ["shape", "count_changed", "short_page", "repeated", "overlap"])
def test_statswales_bad_pages_abort_publication(tmp_path, kind):
    rows = source_fixture()

    def handler(request):
        page = int(request.url.params["page_number"])
        if kind == "shape":
            return httpx.Response(200, json={"data": "oops"})
        batch = rows[:2] if page == 1 else rows[2:]
        count = 3
        if page == 2:
            if kind == "count_changed":
                count = 4
            elif kind == "short_page":
                batch = []
            elif kind == "repeated":
                batch, count = rows[:2], 4
            elif kind == "overlap":
                batch = rows[:1]
        return httpx.Response(200, json={"data": batch, "count": count})

    plan = {"schema_version": 1, "statswales": {"enabled": True, "page_size": 2}}
    with pytest.raises(ProviderHarvestError):
        run(tmp_path, handler, plan)
    assert not LiteratureStore(tmp_path).list()


def test_bad_rows_are_quarantined_with_source_and_raw_location(tmp_path):
    document = json.loads(json.dumps(NOMIS))
    document["structure"]["keyfamilies"]["keyfamily"].append({"id": "broken"})
    result, doc = run(
        tmp_path,
        lambda _: httpx.Response(200, json=document),
        {"schema_version": 1, "nomis": {"enabled": True}},
    )
    assert result["counts"]["datasets"] == 1 and result["counts"]["quarantined"] == 1
    row = doc["quarantine"][0]
    assert row["raw_row"] == {"id": "broken"}
    assert row["references"] and row["source_ids"]
    assert row["location"].endswith("/1")


def test_statswales_targeted_metadata_dimensions_keep_raw_evidence(tmp_path):
    calls = []
    rows = [{"id": IDS[0], "title": "Poverty in Wales"}, {"id": IDS[1], "title": "Fire incidents"}]

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v2":
            return httpx.Response(200, json={"data": rows, "count": 2})
        if request.url.path.endswith("/filters"):
            return httpx.Response(
                200,
                json=[
                    {
                        "columnName": "Area",
                        "factTableColumn": "AreaID",
                        "values": [{"reference": "Wales", "children": [{"reference": "Cardiff"}]}],
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": IDS[0],
                "published_revision": {
                    "id": "rev-1",
                    "coverage_start_date": "2000-01-01",
                    "metadata": [
                        {"language": "cy-GB", "summary": "Welsh summary"},
                        {"language": "en-GB", "summary": "English poverty summary"},
                    ],
                },
            },
        )

    plan = {
        "schema_version": 1,
        "statswales": {
            "enabled": True,
            "page_size": 2,
            "metadata_sample": 1,
            "metadata_keywords": ["poverty"],
            "dimensions": True,
        },
    }
    result, doc = run(tmp_path, handler, plan)
    assert len(calls) == 3 and all("/data" not in path for path in calls)
    row = next(row for row in doc["datasets"] if row["native_id"] == IDS[0])
    descriptor = next(row for row in doc["catalogue_descriptors"] if row["native_id"] == IDS[0])
    assert row["description"] == "English poverty summary" and len(row["references"]) == 3
    assert len(row["source_ids"]) == 3
    assert descriptor["release"]["revision_id"] == "rev-1"
    assert descriptor["columns"] == [
        {"name": "AreaID", "label": "Area", "role": "dimension", "declared_value_count": 2}
    ]
    assert result["coverage"]["statswales"]["dimension_declarations_present"] == 1
    assert descriptor["source_metadata"]["dimension_metadata_resource"].endswith(".json")


def test_govuk_release_attachment_metadata_does_not_download_files(tmp_path):
    calls = []
    document = {
        "title": "English indices of deprivation 2019",
        "base_path": "/government/statistics/english-indices-of-deprivation-2019",
        "first_published_at": "2019-09-26",
        "details": {
            "attachments": [
                {
                    "title": "LSOA ranks",
                    "url": "https://assets.publishing.service.gov.uk/ranks.xlsx",
                    "content_type": "application/xlsx",
                    "file_size": 1000,
                },
                {"title": "Malformed", "url": "http://unsafe.example/file"},
            ]
        },
    }

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=document)

    result, doc = run(tmp_path, handler, {"schema_version": 1, "deprivation": {"years": [2019]}})
    assert len(calls) == 1 and calls[0].url.host == "www.gov.uk"
    assert result["counts"]["datasets"] == 2 and result["counts"]["quarantined"] == 1
    assert doc["catalogue_descriptors"][0]["release"]["year"] == 2019
    assert doc["catalogue_descriptors"][0]["source_metadata"]["location"] == ""
    assert all(
        row["references"] and row["coverage"]["country"] == "England" for row in doc["datasets"]
    )
    assert not result["coverage"]["deprivation"]["data_files_downloaded"]


@pytest.mark.parametrize(
    "url,accepted",
    [
        ("/government/statistics/english-indices-of-deprivation-2025/statistical-release", True),
        ("//other.example/file", False),
        ("/government/statistics/../file", False),
        ("/government/statistics/%2e%2e/file", False),
        ("/government/statistics/%5cother.example/file", False),
        ("relative/unknown", False),
    ],
)
def test_govuk_relative_attachment_urls_are_normalized_without_file_fetch(tmp_path, url, accepted):
    calls = []
    release_path = "/government/statistics/english-indices-of-deprivation-2025"
    document = {
        "title": "English indices of deprivation 2025",
        "base_path": release_path,
        "details": {"attachments": [{"title": "Statistical release", "url": url}]},
    }

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=document)

    result, doc = run(tmp_path, handler, {"schema_version": 1, "deprivation": {"years": [2025]}})
    assert len(calls) == 1 and calls[0].url.path.startswith("/api/content/")
    assert result["counts"]["datasets"] == 1 + int(accepted)
    assert result["counts"]["quarantined"] == int(not accepted)
    if accepted:
        descriptor = next(
            row for row in doc["catalogue_descriptors"] if ":attachment-" in row["native_id"]
        )
        assert descriptor["source_url"] == "https://www.gov.uk" + url
        assert descriptor["source_metadata"]["original_attachment_url"] == url
        assert (
            descriptor["source_metadata"]["attachment_url_normalization"]
            == "leading_single_slash_against_govuk_origin"
        )
        assert descriptor["references"] and descriptor["source_metadata"]["resource_path"]


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": 1},
        {"schema_version": 1, "nomis": []},
        {"schema_version": 1, "nomis": {"enabled": "yes"}},
        {"schema_version": 1, "statswales": {"enabled": True, "page_size": 1001}},
        {"schema_version": 1, "statswales": {"enabled": True, "metadata_sample": -1}},
        {"schema_version": 1, "statswales": {"enabled": True, "metadata_keywords": [1]}},
        {"schema_version": 1, "deprivation": {"years": [2019, 2019]}},
        {"schema_version": 1, "deprivation": {"years": [2020]}},
        {"schema_version": 1, "nomis": {"enabled": True}, "max_requests": 0},
    ],
)
def test_invalid_plan_rejected_before_contact(tmp_path, plan):
    with (
        http_for(tmp_path, lambda _: pytest.fail("Invalid plan contacted provider")) as http,
        pytest.raises(ProviderHarvestError),
    ):
        harvest_provider_metadata(tmp_path, plan, client=MetadataClient(tmp_path, client=http))
