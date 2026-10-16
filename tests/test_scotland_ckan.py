"""Invented current-portal CKAN records; no copied publisher metadata.

Protocol: https://data.gov.scot/help/technical-support/how-to-access-data-using-the-api
"""

import copy
import socket
from unittest.mock import patch

import httpx
import pytest

from ssdu import Connector, Request
from ssdu.adapters import scotland
from ssdu.http import HttpClient
from ssdu.metadata_mapping import MAPPING_VERSION
from ssdu.metadata_store import MetadataStore
from ssdu.metadata_sync import MetadataSync
from ssdu.models import RetrievalError
from ssdu.providers import get_provider
from ssdu.rate_limit import RateLimiter

UUID = "00000000-0000-4000-8000-000000000091"
OTHER_UUID = "00000000-0000-4000-8000-000000000092"
DATASET = "ckan:" + UUID


def native(identity=UUID, name="invented_coastal_tables"):
    """Provider-shaped fields with independently invented assertions."""
    return {
        "id": identity,
        "name": name,
        "title": "Invented coastal events 1800–2200",
        "description": "<p>Invented events.</p><script>hidden()</script><p>Methods.</p>",
        "notes": "This CKAN fallback must not replace the native description.",
        "private": False,
        "state": "active",
        "type": "dataset",
        "isopen": True,
        "organization": {"id": "synthetic-publisher", "title": "Invented Statistics Office"},
        "publisher": [],
        "theme": ["invented-events"],
        "tags": [{"name": "coastal", "display_name": "Coastal research"}],
        "geographical_coverage": ["SYNTHETIC-LEVEL-1"],
        "reference_period": [{"start": "2021-Q1", "end": "2025-Q2"}],
        "spatial_coverage": [],
        "temporal_coverage": [],
        "metadata_created": "2026-08-20T08:00:00",
        "metadata_modified": "2026-08-25T08:00:00",
        "last_updated": "2026-08-24T08:00:00",
        "license_url": "https://example.invalid/synthetic/licence",
        "license_title": "Invented terms",
        "license_id": "synthetic-terms",
        "language": ["en-GB"],
        "resources": [
            {
                "id": "synthetic-resource",
                "title": "Invented resource",
                "url": "https://example.invalid/no-download.csv",
                "format": "CSV",
                "file_size": "128",
                "dictionary": [{"name": "count", "datatype": "integer"}],
            }
        ],
        "metadata": {"croissant": "https://example.invalid/no-follow.jsonld"},
    }


def response(records=None):
    records = records or [native()]

    def handle(request):
        assert request.url.host == "api.data.gov.scot"
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "package_search":
            assert request.url.params["sort"] == "name asc"
            start = int(request.url.params["start"])
            size = int(request.url.params["rows"])
            result = {"count": len(records), "results": records[start : start + size]}
        elif action == "package_list":
            result = [record["name"] for record in records]
        else:
            assert action == "package_show"
            identity = request.url.params["id"]
            result = next(
                record for record in records if identity in {record["id"], record["name"]}
            )
        return httpx.Response(200, json={"success": True, "result": result})

    return handle


def client(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        rate_limiter=RateLimiter(tmp_path / "rates.sqlite3", interval_seconds=0),
        allowed_hosts=get_provider("scotland").allowed_hosts,
    )


def test_current_catalogue_includes_file_records_and_distinct_stable_ids(tmp_path):
    records = [native(), native(OTHER_UUID, "invented_second")]
    with client(tmp_path, response(records)) as transport:
        first = scotland.list_datasets("", transport, limit=1)
        final = scotland.list_datasets("", transport, limit=1, offset=first["next_offset"])
    assert first["items"][0]["id"] == DATASET
    assert final["items"][0]["id"] == "ckan:" + OTHER_UUID
    assert first["total"] == final["total"] == 2
    assert final["next_offset"] is None
    assert first["catalogue_scope"] == "data.gov.scot_public_ckan_catalogue"


def test_metadata_normalizes_assertions_retains_native_payload_and_never_fetches_files(tmp_path):
    source = native()
    with client(tmp_path, response([source])) as transport:
        result = scotland.describe_dataset(DATASET, transport)
        assert len(transport.resources) == 1
    assert result["description"] == "Invented events.\n\nMethods."
    assert result["source_metadata"] == source
    assert result["publishers"] == [
        {"id": "synthetic-publisher", "name": "Invented Statistics Office"}
    ]
    assert result["languages"] == ["en-GB"]
    assert {item["scheme"] for item in result["subjects"]} == {
        "data.gov.scot/theme",
        "data.gov.scot/tag",
    }
    assert result["coverage"]["temporal"]["periods"] == [{"start": "2021-Q1", "end": "2025-Q2"}]
    assert result["coverage"]["spatial"]["areas"] == [{"id": "SYNTHETIC-LEVEL-1"}]
    assert result["licence"] == source["license_url"]
    assert result["dimensions"] == result["measures"] == []
    assert result["distributions"][0]["supported"] is False
    assert result["distributions"][0]["bytes"] == 128
    assert result["distributions"][0]["url"] == source["resources"][0]["url"]
    assert result["related"] == [
        {
            "relation": "alternate_metadata",
            "title": "croissant",
            "url": source["metadata"]["croissant"],
        }
    ]
    assert result["release"]["modified"] == source["metadata_modified"]
    assert "migration" in result["release"]["timestamp_limitations"]
    assert result["metadata_mapping"]["version"] == MAPPING_VERSION
    assert all(
        item["source_path"].startswith("/result/")
        for item in result["metadata_mapping"]["evidence"]
    )
    assert result["field_status"]["coverage.temporal"]["status"] == "mapped"
    assert result["field_status"]["measures"]["status"] == "not_provided"


def test_missing_metadata_does_not_infer_roles_dates_rights_or_english_from_title(tmp_path):
    source = {
        "id": UUID,
        "name": "invented_sparse",
        "title": "Scotland 1800 to 2200 counts",
        "resources": [],
    }
    with client(tmp_path, response([source])) as transport:
        result = scotland.describe_dataset(DATASET, transport)
    assert result["description"] is result["coverage"] is result["licence"] is None
    assert result["publishers"] == result["languages"] == result["subjects"] == []
    assert result["measures"] == result["dimensions"] == []
    assert result["release"]["metadata_modified"] is None
    assert result["access"]["data_access"] == "not_asserted"


def test_unmapped_native_fields_are_distinguished_from_provider_omissions(tmp_path):
    source = native()
    source.update(
        organization={"unexpected_native_name": "Invented publisher"},
        theme={"unexpected_native_subject": "events"},
        language={"unexpected_native_language": "en"},
        reference_period=[{"unexpected_native_period": "2025"}],
        access_right="<p>Invented registration conditions.</p>",
    )
    with client(tmp_path, response([source])) as transport:
        result = scotland.describe_dataset(DATASET, transport)
    for field in ("publishers", "languages", "coverage.temporal"):
        assert result["field_status"][field]["status"] == "unmapped"
    assert result["source_metadata"] == source
    assert result["access"]["conditions"] == "Invented registration conditions."


def test_metadata_only_ids_fail_acquisition_and_dimension_values_without_http(tmp_path):
    with pytest.raises(RetrievalError, match="metadata-only"):
        scotland.validate_request(Request("scotland", DATASET))
    with client(tmp_path, lambda _: pytest.fail("Unexpected HTTP")) as transport:
        with pytest.raises(RetrievalError, match="does not assert"):
            scotland.list_dimension_values(DATASET, "invented", transport)
        with pytest.raises(RetrievalError, match="metadata-only"):
            scotland.fetch(Request("scotland", DATASET), transport)


def test_full_inventory_reconciles_independent_names_and_declares_non_atomic_scope(tmp_path):
    records = [native(), native(OTHER_UUID, "invented_second")]
    with client(tmp_path, response(records)) as transport:
        result = scotland.catalogue_inventory("", transport, max_records=10)
    assert result["inventory_complete"]
    assert result["reconciliation"]["matched"]
    assert result["reconciliation"]["atomic_upstream_snapshot"] is False
    assert result["reconciliation"]["expected_count"] == 2
    assert result["errors"] == []


def test_capped_inventory_is_explicitly_partial_and_does_not_claim_reconciliation(tmp_path):
    records = [native(), native(OTHER_UUID, "invented_second")]
    with client(tmp_path, response(records)) as transport:
        result = scotland.catalogue_inventory("", transport, max_records=1)
        assert len(transport.resources) == 1
    assert result["inventory_complete"] is False
    assert result["next_offset"] == 1
    assert result["reconciliation"]["method"] == "declared_count"


def test_changed_inventory_membership_is_reported_without_false_complete_claim(tmp_path):
    def handler(request):
        if request.url.path.endswith("package_list"):
            return httpx.Response(200, json={"success": True, "result": ["invented_replacement"]})
        return response()(request)

    with client(tmp_path, handler) as transport:
        result = scotland.catalogue_inventory("", transport)
    assert result["inventory_complete"] is False
    assert result["errors"][0]["code"] == "membership_changed_or_incomplete"


@pytest.mark.parametrize(
    "defect", ["duplicate", "overflow", "private", "malformed_id", "wrong_count", "empty_early"]
)
def test_malformed_catalogue_pages_fail_closed(tmp_path, defect):
    values = [native()]
    count = 1
    if defect == "duplicate":
        values *= 2
        count = 2
    elif defect == "overflow":
        values.append(native(OTHER_UUID, "invented_second"))
        count = 2
    elif defect == "private":
        values[0]["private"] = True
    elif defect == "malformed_id":
        values[0]["id"] = "unsafe/native/path"
    elif defect == "wrong_count":
        count = True
    else:
        values = []
    handler = lambda _: httpx.Response(
        200, json={"success": True, "result": {"count": count, "results": values}}
    )
    with client(tmp_path, handler) as transport, pytest.raises(RetrievalError):
        scotland.list_datasets("", transport, limit=1 if defect == "overflow" else 2)


@pytest.mark.parametrize(
    "defect", ["different_id", "failure", "duplicate_resources", "unsafe_urls"]
)
def test_description_rejects_identity_protocol_errors_and_does_not_follow_unsafe_urls(
    tmp_path, defect
):
    source = copy.deepcopy(native())
    if defect == "different_id":
        source["id"] = OTHER_UUID
    elif defect == "duplicate_resources":
        source["resources"] *= 2
    elif defect == "unsafe_urls":
        source["resources"][0]["url"] = "javascript:unsafe()"
        source["metadata"]["croissant"] = "file:///private/invented"
    handler = lambda _: httpx.Response(200, json={"success": defect != "failure", "result": source})
    with client(tmp_path, handler) as transport:
        if defect == "unsafe_urls":
            result = scotland.describe_dataset(DATASET, transport)
            assert "url" not in result["distributions"][0]
            assert result["related"] == []
            assert result["source_metadata"] == source
        else:
            with pytest.raises(RetrievalError):
                scotland.describe_dataset(DATASET, transport)


def test_current_connector_cache_replays_offline_without_rate_slot_or_socket(tmp_path):
    with client(tmp_path, response()) as transport:
        connector = Connector("scotland", tmp_path / "store", client=transport)
        page = connector.list_datasets()
        metadata = connector.describe_dataset(DATASET)
        assert len(transport.resources) == 2
        with (
            patch.object(HttpClient, "request", side_effect=AssertionError("Offline HTTP")),
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("Offline DNS")),
            patch.object(
                RateLimiter, "slot", side_effect=AssertionError("Cached request used quota")
            ),
        ):
            assert connector.list_datasets(offline=True) == page
            assert connector.describe_dataset(DATASET, offline=True) == metadata
    assert "scotland_ckan.py" in {
        path.name for path in get_provider("scotland").implementation_paths()
    }


def test_current_metadata_snapshot_exports_imports_and_replays_in_fresh_offline_store(tmp_path):
    root = tmp_path / "source"

    def factory(**kwargs):
        return HttpClient(
            transport=httpx.MockTransport(response()),
            rate_limiter=RateLimiter(tmp_path / "rates.sqlite3", interval_seconds=0),
            **kwargs,
        )

    configuration = {"providers": {"scotland": {"dataset_ids": [DATASET], "detail": "dataset"}}}
    sync = MetadataSync(root, client_factory=factory)
    plan = sync.plan_sync(configuration)
    job = sync.run_job(sync.submit_plan(plan["plan_id"], "current-scotland")["job_id"])
    assert job["status"] == "succeeded", job
    snapshot_id = job["result"]["snapshot_id"]
    store = MetadataStore(root)
    before = store.read(snapshot_id)
    assert len(before["records"]) == 1
    record = before["records"][0]
    assert record["publishers"][0]["name"] == "Invented Statistics Office"
    assert record["distributions"][0]["supported"] is False
    bundle = store.export(snapshot_id, tmp_path / "invented-scotland.metadata.zip")
    root.rename(tmp_path / "source-retained")
    fresh_root = tmp_path / "fresh"
    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Offline HTTP")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("Offline DNS")),
        patch.object(socket.socket, "connect", side_effect=AssertionError("Offline socket")),
    ):
        fresh = MetadataStore(fresh_root)
        fresh.import_bundle(bundle)
        assert fresh.read(snapshot_id) == before
        replay = MetadataSync(fresh_root)
        plan = replay.plan_sync(configuration, offline=True)
        replay_job = replay.run_job(
            replay.submit_plan(plan["plan_id"], "offline-current-scotland")["job_id"]
        )
        assert replay_job["status"] == "succeeded", replay_job
        assert replay_job["requests"] == replay_job["bytes"] == 0
        after = fresh.read(replay_job["result"]["snapshot_id"])
        assert after["records"] == before["records"]
