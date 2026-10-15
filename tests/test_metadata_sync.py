"""Durable metadata ingestion uses independently invented protocol responses only."""

import copy
import importlib.util
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_store import MetadataStore
from ssdu.metadata_sync import MetadataSync
from ssdu.rate_limit import RateLimiter

MODULE = importlib.util.spec_from_file_location(
    "metadata_teaching_fixture", Path(__file__).resolve().parents[1] / "examples/metadata_sync.py"
)
FIXTURE = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(FIXTURE)


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


def sync_for(root, handler=None, clock=None):
    clock = clock or Clock()
    calls = []

    def transport(request):
        calls.append(request)
        return (handler or FIXTURE.fixture_response)(request)

    def factory(**kwargs):
        return HttpClient(
            transport=httpx.MockTransport(transport),
            rate_limiter=RateLimiter(
                root / "rates.sqlite3", interval_seconds=0, max_wait_seconds=0, clock=clock
            ),
            **kwargs,
        )

    return MetadataSync(root, clock=clock, client_factory=factory), calls, clock


def spec(provider="nomis", **kwargs):
    return {"providers": {provider: {"detail": "codes", "page_size": 20, **kwargs}}}


def submit(sync, configuration=None, key="test", **kwargs):
    plan = sync.plan_sync(configuration or spec(), **kwargs)
    return sync.submit_plan(plan["plan_id"], key)["job_id"]


def snapshot(sync, job_id):
    job = sync.run_job(job_id)
    assert job["status"] == "succeeded", job
    return job, MetadataStore(sync.store.root).read(job["result"]["snapshot_id"])


@pytest.mark.parametrize("provider", FIXTURE.DATASETS)
def test_each_provider_sync_keeps_raw_references_and_metadata_only(provider, tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    job_id = submit(sync, spec(provider))
    assert calls == []
    job, document = snapshot(sync, job_id)
    assert document["contract_version"] == "1.0.0"
    assert len(document["records"]) == 1
    row = document["records"][0]
    assert row["dataset_id"] == f"{provider}:{FIXTURE.DATASETS[provider]}"
    assert row["title"] == FIXTURE.TITLES[provider]
    assert row["references"] and row["raw_resources"]
    assert row["last_checked_at"]
    report = document["providers"][0]
    assert report["completeness"]["status"] == "complete"
    assert not report["completeness"]["inventory_authoritative"]
    assert report["completeness"]["codes_complete"]
    assert job["requests"] == len(calls) > 0
    assert job["bytes"] > 0
    manifest = MetadataStore(tmp_path).verify(job["result"]["snapshot_id"])
    assert all("resources/" + path in manifest["files"] for path in row["raw_resources"])
    assert (
        MetadataIndex(tmp_path).get(row["dataset_id"])["metadata_snapshot_id"]
        == job["result"]["snapshot_id"]
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"providers": {}},
        {"providers": {"unknown": {}}},
        {"providers": {"deprivation": {}}},
        {"providers": {"nomis": {"extra": True}}},
        spec(page_size=True),
        spec(page_size=101),
        spec(max_pages=0),
        spec(max_datasets=-1),
        spec(max_code_pages=1001),
        spec(dataset_ids=["a", "a"]),
        spec(dataset_ids=["a"], query="foo"),
        spec(detail="observations"),
        spec(detail="catalogue", dataset_ids=["a"]),
    ],
)
def test_invalid_local_plans_do_not_contact_providers(invalid, tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    with pytest.raises(ValueError):
        sync.plan_sync(invalid)
    assert calls == [] and sync.list_jobs()["total"] == 0


def test_no_implicit_fetch_and_idempotency_and_cancel(tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    plan = sync.plan_sync(spec())
    assert sync.get_plan(plan["plan_id"]) == plan
    first = sync.submit_plan(plan["plan_id"], "one")
    assert sync.submit_plan(plan["plan_id"], "one")["job_id"] == first["job_id"]
    other = sync.plan_sync(spec(detail="catalogue"))
    with pytest.raises(ValueError, match="another metadata plan"):
        sync.submit_plan(other["plan_id"], "one")
    cancelled = sync.cancel_job(first["job_id"])
    assert sync.run_job(first["job_id"])["status"] == cancelled["status"] == "cancelled"
    assert sync.run_job()["status"] == "idle"
    assert sync.list_jobs()["items"][0]["job_id"] == first["job_id"]
    assert calls == []
    with pytest.raises(ValueError):
        sync.plan_sync(spec(), offline=True, refresh=True)
    with pytest.raises(ValueError):
        sync.list_jobs(limit=True)


def test_explicit_ids_define_bounded_non_inventory_scope(tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    _, document = snapshot(sync, submit(sync, spec(dataset_ids=[FIXTURE.DATASETS["nomis"]])))
    assert all("/dataset/def.sdmx.json" not in str(call.url) for call in calls)
    report = document["providers"][0]
    assert report["completeness"]["status"] == "complete"
    assert not report["completeness"]["catalogue_complete"]
    assert not report["completeness"]["inventory_authoritative"]


def two_dataset_handler(provider):
    first = FIXTURE.DATASETS[provider]
    second = {
        "nomis": "NM_999_3",
        "statswales": "00000000-0000-4000-8000-000000000002",
        "mhclg": "ssdu-invented-rural",
        "nisra": "SYNTH_RURAL",
        "scotland": "ssdu-invented-rural",
    }[provider]
    ids = [first, second]

    def handler(request):
        path, params = request.url.path, request.url.params
        if provider == "nomis" and path.endswith("/dataset/def.sdmx.json"):
            value = FIXTURE.nomis_definition()
            other = copy.deepcopy(value["structure"]["keyfamilies"]["keyfamily"][0])
            other["id"], other["name"]["value"] = second, "Invented rural occupations"
            value["structure"]["keyfamilies"]["keyfamily"].append(other)
            return httpx.Response(200, json=value)
        if provider == "statswales" and path == "/v2":
            size, offset = (
                int(params["page_size"]),
                (int(params["page_number"]) - 1) * int(params["page_size"]),
            )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": identity, "title": "Invented catalogue"}
                        for identity in ids[offset : offset + size]
                    ],
                    "total": 2,
                },
            )
        if provider == "mhclg" and path == "/api/search.json":
            size, offset = int(params["count"]), int(params["start"])
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Invented catalogue",
                            "link": "/government/statistics/" + identity,
                        }
                        for identity in ids[offset : offset + size]
                    ],
                    "total": 2,
                },
            )
        if provider == "nisra" and path.endswith("ReadCollection"):
            return httpx.Response(
                200,
                json={
                    "version": "2.0",
                    "class": "collection",
                    "link": {
                        "item": [
                            {"label": "Invented catalogue", "extension": {"matrix": identity}}
                            for identity in ids
                        ]
                    },
                },
            )
        if provider == "scotland" and "GROUP BY ?dataset" in params.get("query", ""):
            size, offset = map(
                int, re.search(r"LIMIT (\d+) OFFSET (\d+)", params["query"]).groups()
            )
            return httpx.Response(
                200,
                json=FIXTURE.bindings(
                    ["dataset", "title"],
                    [
                        {
                            "dataset": FIXTURE.term(FIXTURE.SCOT_BASE + identity, "uri"),
                            "title": FIXTURE.term("Invented catalogue"),
                        }
                        for identity in ids[offset : offset + size]
                    ],
                ),
            )
        # Clone the independently invented definition to a second native identity.
        original = str(request.url).replace(second, first)
        if provider == "scotland":
            original = str(
                request.url.copy_set_param("query", params["query"].replace(second, first))
            )
        response = FIXTURE.fixture_response(httpx.Request(request.method, original))
        body = response.content.replace(
            first.encode(), (second if second in str(request.url) else first).encode()
        )
        return httpx.Response(response.status_code, content=body, headers=response.headers)

    return handler, ids


@pytest.mark.parametrize("provider", FIXTURE.DATASETS)
def test_common_pagination_and_bounds_across_unrelated_datasets(provider, tmp_path):
    handler, identities = two_dataset_handler(provider)
    sync, calls, _ = sync_for(tmp_path, handler)
    _, first = snapshot(
        sync, submit(sync, spec(provider, detail="dataset", page_size=1, max_pages=1), "partial")
    )
    report = first["providers"][0]["completeness"]
    assert report["status"] == "partial" and not report["catalogue_complete"]
    assert not report["inventory_authoritative"] and report["discovered_count"] == 1
    _, full = snapshot(
        sync, submit(sync, spec(provider, detail="dataset", page_size=1, max_pages=3), "full")
    )
    assert {row["native_id"] for row in full["records"]} == set(identities)
    assert full["providers"][0]["completeness"]["status"] == "complete"
    assert len(calls) > 0


def test_code_bound_is_partial_and_not_current_after_cache_replay(tmp_path):
    sync, calls, clock = sync_for(tmp_path)
    _, first = snapshot(sync, submit(sync, spec(page_size=1, max_code_pages=1)))
    report = first["providers"][0]
    assert report["completeness"]["catalogue_complete"]
    assert report["completeness"]["descriptions_complete"]
    assert not report["completeness"]["codes_complete"]
    assert report["completeness"]["status"] == "partial"
    checked = report["freshness"]["last_checked_at"]
    before = len(calls)
    clock.now += 100_000
    replay, second = snapshot(
        sync, submit(sync, spec(page_size=1, max_code_pages=1), "replay", offline=True)
    )
    assert len(calls) == before and replay["requests"] == replay["bytes"] == 0
    assert second["providers"][0]["freshness"]["last_checked_at"] == checked
    assert second["providers"][0]["freshness"]["captured_at"] == report["freshness"]["captured_at"]
    assert second["captured_at"] != first["captured_at"]
    assert second["records"][0]["captured_at"] == first["records"][0]["captured_at"]


def test_interrupted_multi_request_operation_restarts_worker_without_refetching_prefix(tmp_path):
    interrupted, calls = False, []

    class Stop(BaseException):
        pass

    def handler(request):
        nonlocal interrupted
        calls.append(str(request.url))
        if request.url.path.endswith("/filters") and not interrupted:
            interrupted = True
            raise Stop()
        return FIXTURE.fixture_response(request)

    sync, _, clock = sync_for(tmp_path, handler)
    job_id = submit(sync, spec("statswales", detail="dataset"), refresh=True)
    with pytest.raises(Stop):
        sync.run_job(job_id)
    assert sync.get_job(job_id)["status"] == "queued"
    prefix = calls[:-1]
    sync, _, _ = sync_for(tmp_path, handler, clock)
    job, _ = snapshot(sync, job_id)
    assert job["providers"]["statswales"]["attempts"] == 1
    assert not set(prefix).intersection(calls[len(prefix) + 1 :])


def test_cooldown_is_durable_and_failed_attempts_count(tmp_path):
    first = True

    def handler(request):
        nonlocal first
        if first:
            first = False
            return httpx.Response(429, headers={"Retry-After": "60"})
        return FIXTURE.fixture_response(request)

    sync, calls, clock = sync_for(tmp_path, handler)
    job_id = submit(sync, spec())
    waiting = sync.run_job(job_id)
    assert waiting["status"] == "waiting" and waiting["requests"] == 1
    assert waiting["retry_at"] >= clock.now + 60
    assert MetadataStore(tmp_path).list()["total"] == 0
    assert sync.run_job(job_id)["status"] == "waiting" and len(calls) == 1
    clock.now = waiting["retry_at"]
    resumed, resumed_calls, _ = sync_for(tmp_path, handler, clock)
    job, _ = snapshot(resumed, job_id)
    assert job["requests"] == len(calls) + len(resumed_calls) == 4


@pytest.mark.parametrize("budget", [{"max_requests": 1}, {"max_total_bytes": 1}])
def test_cumulative_budgets_fail_without_publishing_and_cannot_reset_on_retry(budget, tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    job_id = submit(sync, spec(), budget=budget)
    job = sync.run_job(job_id)
    assert job["status"] == "failed" and job["error"]["code"] == "budget_exhausted"
    assert job["requests"] == len(calls) == 1
    assert MetadataStore(tmp_path).list()["total"] == 0
    with pytest.raises(ValueError, match="budget exhausted"):
        sync.retry_job(job_id)


def test_invalid_description_is_quarantined_with_raw_evidence(tmp_path):
    def handler(request):
        if request.url.path.endswith("/NM_999_2/def.sdmx.json"):
            return httpx.Response(200, json={"invented_invalid_metadata": "retained for diagnosis"})
        return FIXTURE.fixture_response(request)

    sync, _, _ = sync_for(tmp_path, handler)
    _, document = snapshot(sync, submit(sync))
    report = document["providers"][0]["completeness"]
    assert report["status"] == "partial" and report["quarantine_count"] == 1
    assert report["discovered_count"] == 1 and report["described_count"] == 0
    row = document["records"][0]
    assert row["source_metadata"]["descriptor"] is None
    assert len(row["raw_resources"]) == 2


def test_changed_metadata_publishes_new_snapshot_without_modifying_old(tmp_path):
    changed = False

    def handler(request):
        value = FIXTURE.fixture_response(request)
        if changed and request.url.host == "www.nomisweb.co.uk":
            return httpx.Response(
                200,
                content=value.content.replace(
                    FIXTURE.TITLES["nomis"].encode(), b"Invented revised occupation metadata"
                ),
                headers=value.headers,
            )
        return value

    sync, calls, clock = sync_for(tmp_path, handler)
    old_job, old = snapshot(sync, submit(sync))
    changed, clock.now = True, clock.now + 100
    new_job, new = snapshot(sync, submit(sync, key="changed", refresh=True))
    assert old_job["result"]["snapshot_id"] != new_job["result"]["snapshot_id"]
    assert old["records"][0]["title"] == FIXTURE.TITLES["nomis"]
    assert new["records"][0]["title"] == "Invented revised occupation metadata"
    assert MetadataStore(tmp_path).read(old_job["result"]["snapshot_id"]) == old
    assert len(calls) == 7  # refresh reacquires the repeated definition for codes


def test_corrupt_checkpoint_fails_closed_and_clean_retry_reacquires(tmp_path):
    class Stop(BaseException):
        pass

    stopped = False

    def handler(request):
        nonlocal stopped
        if request.url.path.endswith("/NM_999_2/def.sdmx.json") and not stopped:
            stopped = True
            raise Stop()
        return FIXTURE.fixture_response(request)

    sync, calls, _ = sync_for(tmp_path, handler)
    job_id = submit(sync)
    with pytest.raises(Stop):
        sync.run_job(job_id)
    checkpoint = next((sync.root / "checkpoints").rglob("00000.bin"))
    checkpoint.write_bytes(b"Corruption")
    failed = sync.run_job(job_id)
    assert failed["status"] == "failed"
    assert MetadataStore(tmp_path).list()["total"] == 0
    assert sync.retry_job(job_id)["providers"]["nomis"]["epoch"] == 1
    completed, _ = snapshot(sync, job_id)
    assert completed["requests"] == len(calls)


def test_no_cache_offline_fails_with_no_http_or_inventory(tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    with patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")):
        result = sync.run_job(submit(sync, offline=True))
    assert result["status"] == "failed" and result["requests"] == result["bytes"] == 0
    assert calls == [] and MetadataStore(tmp_path).list()["total"] == 0


@pytest.mark.parametrize("change", ["provider", "dataset_id", "title", "dimensions", "shape"])
def test_mismatched_or_malformed_description_never_leaks_into_catalogue_record(change, tmp_path):
    sync, _, _ = sync_for(tmp_path)
    invalid = {
        "provider": "nomis",
        "dataset_id": FIXTURE.DATASETS["nomis"],
        "title": "Wrong unreconciled description",
        "dimensions": [],
    }
    invalid[change] = {
        "provider": "nisra",
        "dataset_id": "NM_999_999",
        "title": "",
        "dimensions": {},
        "shape": False,
    }[change]
    if change == "shape":
        invalid = []
    with patch("ssdu.adapters.nomis.describe_dataset", return_value=invalid):
        _, document = snapshot(sync, submit(sync))
    row = document["records"][0]
    assert row["title"] == FIXTURE.TITLES["nomis"]
    assert row["source_metadata"]["descriptor"] is None
    assert document["providers"][0]["completeness"]["status"] == "partial"


def test_conditional_refresh_costs_one_request_zero_bytes_and_retains_original_capture(tmp_path):
    from ssdu.metadata_contract import record_version

    def handler(request):
        if request.headers.get("if-none-match") == '"invented-revision"':
            return httpx.Response(304, headers={"etag": '"invented-revision"'})
        response = FIXTURE.fixture_response(request)
        return httpx.Response(
            200,
            content=response.content,
            headers={"content-type": "application/json", "etag": '"invented-revision"'},
        )

    sync, calls, clock = sync_for(tmp_path, handler)
    configuration = spec(detail="catalogue")
    _, original = snapshot(sync, submit(sync, configuration))
    clock.now += 500
    job, refreshed = snapshot(
        sync,
        submit(
            sync,
            configuration,
            key="revalidated",
            refresh=True,
            budget={"max_requests": 1, "max_total_bytes": 1},
        ),
    )
    assert job["requests"] == 1 and job["bytes"] == 0
    assert len(calls) == 2 and calls[-1].headers["if-none-match"] == '"invented-revision"'
    source = refreshed["source_metadata"]["responses"][0]
    assert source["revalidated"] and source["status_code"] == 200 and not source["cache_hit"]
    assert refreshed["records"][0]["captured_at"] == original["records"][0]["captured_at"]
    assert refreshed["records"][0]["last_checked_at"] != original["records"][0]["last_checked_at"]
    assert record_version(refreshed["records"][0]) == record_version(original["records"][0])


@pytest.mark.parametrize("change", ["duplicate", "total"])
def test_changed_catalogue_pages_cannot_claim_authoritative_inventory(change, tmp_path):
    handler, _ = two_dataset_handler("mhclg")

    def response(request):
        result = handler(request)
        if request.url.path == "/api/search.json" and request.url.params["start"] == "1":
            document = result.json()
            if change == "duplicate":
                document["results"][0]["link"] = (
                    "/government/statistics/" + FIXTURE.DATASETS["mhclg"]
                )
            else:
                document["total"] = 3
            return httpx.Response(200, json=document)
        return result

    sync, _, _ = sync_for(tmp_path, response)
    _, document = snapshot(
        sync, submit(sync, spec("mhclg", detail="catalogue", page_size=1, max_pages=2))
    )
    report = document["providers"][0]["completeness"]
    assert report["status"] == "partial" and not report["catalogue_complete"]
    assert not report["inventory_authoritative"]
    assert report["duplicate_count"] == (1 if change == "duplicate" else 0)


def test_clean_retry_reacquires_failed_catalogue_epoch_without_resetting_budget(tmp_path):
    valid = False

    def handler(request):
        if not valid:
            return httpx.Response(200, json={"invented": "unusable catalogue"})
        return FIXTURE.fixture_response(request)

    sync, calls, _ = sync_for(tmp_path, handler)
    job_id = submit(sync)
    failed = sync.run_job(job_id)
    assert failed["status"] == "failed" and failed["requests"] == 1
    valid = True
    retried = sync.retry_job(job_id)
    assert retried["requests"] == 1 and retried["providers"]["nomis"]["epoch"] == 1
    job, _ = snapshot(sync, job_id)
    assert job["requests"] == len(calls) == 5
    assert job["providers"]["nomis"]["attempts"] == 2


def test_cancel_during_request_stops_at_checkpoint_without_publication(tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    job_id = submit(sync)

    def handler(request):
        sync.cancel_job(job_id)
        return FIXTURE.fixture_response(request)

    replacement, _, _ = sync_for(tmp_path, handler)
    result = replacement.run_job(job_id)
    assert result["status"] == "cancelled"
    assert MetadataStore(tmp_path).list()["total"] == 0
    assert calls == []


def test_replan_required_when_implementation_changes(tmp_path):
    sync, calls, _ = sync_for(tmp_path)
    job_id = submit(sync)
    with patch("ssdu.metadata_sync._implementation", return_value="0" * 64):
        result = sync.run_job(job_id)
    assert result["status"] == "failed" and calls == []
    assert MetadataStore(tmp_path).list()["total"] == 0


def test_same_count_offset_race_preserves_previously_indexed_datasets(tmp_path):
    ids = [f"00000000-0000-4000-8000-{number:012d}" for number in range(1, 7)]
    moving = False

    def handler(request):
        nonlocal moving
        assert request.url.path == "/v2"
        offset = (int(request.url.params["page_number"]) - 1) * 2
        if moving and offset == 0:
            selected = ids[4:6]
            moving = False  # provider changes between pages without changing total
        else:
            selected = ids[offset : offset + 2]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": identity, "title": "Invented live catalogue"} for identity in selected
                ],
                "total": 4,
            },
        )

    sync, _, clock = sync_for(tmp_path, handler)
    configuration = spec("statswales", detail="catalogue", page_size=2, max_pages=3)
    snapshot(sync, submit(sync, configuration))
    moving, clock.now = True, clock.now + 100
    _, moved = snapshot(sync, submit(sync, configuration, key="moving", refresh=True))
    report = moved["providers"][0]["completeness"]
    assert report["status"] == "complete" and report["catalogue_complete"]
    assert not report["inventory_authoritative"]
    index = MetadataIndex(tmp_path)
    assert index.search(provider="statswales")["total"] == 6
    assert index.get("statswales:" + ids[0])["status"] == "active"
    assert index.get("statswales:" + ids[1])["status"] == "active"


def test_cancel_racing_interruption_terminalizes_instead_of_stranding_queued_job(tmp_path):
    class Stop(BaseException):
        pass

    sync, _, _ = sync_for(tmp_path)
    job_id = submit(sync)

    def handler(request):
        sync.cancel_job(job_id)
        raise Stop()

    worker, _, _ = sync_for(tmp_path, handler)
    with pytest.raises(Stop):
        worker.run_job(job_id)
    assert sync.get_job(job_id)["status"] == "cancelled"
    assert sync.run_job(job_id)["status"] == "cancelled"
    assert MetadataStore(tmp_path).list()["total"] == 0


def test_publication_interruption_reuses_frozen_snapshot_identity_without_http(tmp_path):
    class Stop(BaseException):
        pass

    sync, calls, clock = sync_for(tmp_path)
    job_id = submit(sync)
    with patch.object(MetadataIndex, "import_snapshot", side_effect=Stop()), pytest.raises(Stop):
        sync.run_job(job_id)
    retained = MetadataStore(tmp_path).list()["rows"][0]["snapshot_id"]
    before = len(calls)
    clock.now += 100
    job, _ = snapshot(sync, job_id)
    assert job["result"]["snapshot_id"] == retained
    assert MetadataStore(tmp_path).list()["total"] == 1
    assert len(calls) == before
