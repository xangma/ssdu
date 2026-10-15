import copy
import hashlib
import json
import multiprocessing
import os
import signal
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import test_statswales
from test_nomis import definition, page

from ssdu.adapters import deprivation, nomis
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.local_import import import_local
from ssdu.models import Request
from ssdu.rate_limit import RateLimiter
from ssdu.retrieval import RetrievalService
from ssdu.shared import SharedDeprivation
from ssdu.store import canonical

source_fixture = test_statswales.source_fixture


def local_entry(store, tmp_path, value=1):
    path = tmp_path / f"local-{value}.csv"
    path.write_text(f"record_id,value\n001,{value}\n")
    manifest = import_local(store, path, {"dataset_id": "observations", "title": "Observations"})
    path.unlink()
    return {"snapshot_id": manifest["product_id"], "query": {"limit": 10}}


def nomis_entry():
    request = nomis.default_request().to_dict()
    request["page_size"] = 2
    return {"request": request}


def make_job(service, entries, **kwargs):
    plan = service.plan_retrieval({"name": "research", "entries": entries}, **kwargs)
    assert plan["ready"], plan
    return plan, service.submit_plan(plan["plan_id"], "test-run")


def nomis_handler(calls, *, value="12"):
    def handler(request):
        calls.append((request.url.path, request.url.params.get("recordoffset")))
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=definition())
        offset = int(request.url.params["recordoffset"])
        rows = [(0, value, "A"), (1, "..", "S")] if offset == 0 else [(2, "0", "A")]
        return httpx.Response(200, text=page(rows))

    return handler


def factory(handler, **extra):
    return lambda **kwargs: HttpClient(
        transport=httpx.MockTransport(handler), retries=0, **extra, **kwargs
    )


@pytest.fixture
def no_http(monkeypatch):
    monkeypatch.setattr(
        HttpClient, "request", lambda *a, **k: pytest.fail("Unexpected provider HTTP")
    )


def test_local_plan_is_deterministic_pinned_and_offline(tmp_path, no_http):
    service = RetrievalService(tmp_path / "store")
    entry = local_entry(service.store, tmp_path)
    spec = {"name": "research", "entries": {"observations": entry}}
    first = service.plan_retrieval(spec, offline=True)
    assert first == service.plan_retrieval(spec, offline=True)
    assert first["estimated_requests"] == first["estimated_bytes"] == 0
    assert first["entries"]["observations"]["action"] == "snapshot"
    assert first["entries"]["observations"]["query_validation"] == "validated"
    job = service.submit_plan(first["plan_id"], "local-test")
    assert job["status"] == "queued" and not job["requests"]
    done = RetrievalService(service.store).run_job(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["requests"] == done["bytes"] == 0
    assert Collections(service.store).replay(done["result"]["lock_id"])["entries"]["observations"][
        "rows"
    ] == [{"record_id": "001", "value": "1"}]


def test_missing_offline_and_bad_selectors_are_blocked(tmp_path, no_http):
    service = RetrievalService(tmp_path)
    spec = {"name": "research", "entries": {"nomis": nomis_entry()}}
    offline = service.plan_retrieval(spec, offline=True)
    assert not offline["ready"]
    with pytest.raises(ValueError, match="blocked"):
        service.submit_plan(offline["plan_id"], "blocked")
    online = service.plan_retrieval(spec)
    assert online["ready"] and online["estimated_requests"] is None
    assert online["entries"]["nomis"]["selector_validation"] == "pending"
    del spec["entries"]["nomis"]["request"]["params"]["geography"]
    assert service.plan_retrieval(spec)["ready"]
    spec["entries"]["nomis"]["request"]["params"]["invalid selector"] = "x"
    assert not service.plan_retrieval(spec)["ready"]


@pytest.mark.parametrize(
    "params",
    [
        {"scope": "wales"},
        {"local_authority_codes": []},
        {"local_authority_codes": ["E06000044"], "year": 2019},
    ],
)
def test_invalid_deprivation_selection_never_queues(tmp_path, no_http, params):
    service = RetrievalService(tmp_path)
    plan = service.plan_retrieval(
        {
            "name": "research",
            "entries": {
                "areas": {
                    "request": {
                        "provider": "deprivation",
                        "dataset_id": deprivation.DATASET_ID,
                        "params": params,
                    }
                }
            },
        }
    )
    assert not plan["ready"] and plan["entries"]["areas"]["issues"]


def test_submission_concurrency_idempotence_and_plan_binding(tmp_path, no_http):
    service = RetrievalService(tmp_path / "store")
    entry = local_entry(service.store, tmp_path)
    first = service.plan_retrieval({"name": "research", "entries": {"observations": entry}})
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(
            pool.map(
                lambda _: RetrievalService(service.store).submit_plan(
                    first["plan_id"], "shared-key"
                ),
                range(12),
            )
        )
    assert len({j["job_id"] for j in jobs}) == 1
    assert service.list_jobs()["total"] == 1
    second = service.plan_retrieval({"name": "different", "entries": {"observations": entry}})
    with pytest.raises(ValueError, match="another plan"):
        service.submit_plan(second["plan_id"], "shared-key")


def test_tampered_plan_and_implementation_change_fail_closed(tmp_path, monkeypatch):
    service = RetrievalService(tmp_path / "store")
    plan, job = make_job(service, {"observations": local_entry(service.store, tmp_path)})
    monkeypatch.setattr("ssdu.retrieval._implementation", lambda: "0" * 64)
    assert service.submit_plan(plan["plan_id"], "test-run")["job_id"] == job["job_id"]
    assert service.run_job(job["job_id"])["status"] == "failed"
    with pytest.raises(ValueError, match="Implementation"):
        service.submit_plan(plan["plan_id"], "new-key")
    with service._tx() as db:
        bad = copy.deepcopy(plan)
        bad["ready"] = False
        db.execute(
            "UPDATE plans SET document=? WHERE plan_id=?", (json.dumps(bad), plan["plan_id"])
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        service.get_plan(plan["plan_id"])


def test_cache_plan_pins_snapshot_even_if_request_index_changes(tmp_path):
    calls = []
    service = RetrievalService(tmp_path, client_factory=factory(nomis_handler(calls)))
    request = Request(**nomis_entry()["request"])
    with factory(nomis_handler(calls))() as client:
        original = service.store.fetch(request, client=client)
    plan, job = make_job(service, {"nomis": nomis_entry()}, offline=True)
    with factory(nomis_handler(calls, value="99"))() as client:
        newer = service.store.fetch(request, refresh=True, client=client)
    assert newer["product_id"] != original["product_id"]
    assert plan["entries"]["nomis"]["snapshot_id"] == original["product_id"]
    done = service.run_job(job["job_id"])
    assert done["status"] == "succeeded" and done["requests"] == 0
    assert done["entries"]["nomis"]["snapshot_id"] == original["product_id"]


def test_later_cache_hit_needs_no_network_client_or_rate_ledger(tmp_path, monkeypatch):
    service = RetrievalService(tmp_path)
    _, job = make_job(service, {"nomis": nomis_entry()})
    with factory(nomis_handler([]))() as client:
        saved = service.store.fetch(Request(**nomis_entry()["request"]), client=client)
    monkeypatch.setattr(
        service, "_client", lambda *a: pytest.fail("Cached job constructed HTTP client")
    )
    done = service.run_job(job["job_id"])
    assert done["status"] == "succeeded" and done["requests"] == 0
    assert done["entries"]["nomis"]["snapshot_id"] == saved["product_id"]


def test_interrupted_pagination_reuses_http_prefix_and_completed_entries(tmp_path):
    calls = []
    handler = nomis_handler(calls)
    interrupted = [False]

    def interrupt(request):
        if request.url.params.get("recordoffset") == "2" and not interrupted[0]:
            interrupted[0] = True
            calls.append(("interrupted", "2"))
            raise KeyboardInterrupt
        return handler(request)

    service = RetrievalService(tmp_path / "store", client_factory=factory(interrupt))
    _, job = make_job(
        service, {"annotations": local_entry(service.store, tmp_path), "nomis": nomis_entry()}
    )
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job["job_id"])
    partial = service.get_job(job["job_id"])
    assert partial["status"] == "queued"
    assert partial["entries"]["annotations"]["state"] == "succeeded"
    assert partial["entries"]["nomis"]["state"] == "running"
    assert partial["requests"] == 3
    assert len(list((service.root / "checkpoints").rglob("*.json"))) == 2
    resumed = RetrievalService(service.store, client_factory=factory(interrupt)).run_job(
        job["job_id"]
    )
    assert resumed["status"] == "succeeded", resumed
    assert resumed["requests"] == 5
    assert resumed["entries"]["annotations"]["attempts"] == 1
    assert resumed["entries"]["nomis"]["attempts"] == 1
    assert [offset for _, offset in calls].count("0") == 1
    assert sum(path.endswith(".json") for path, _ in calls) == 2


def test_429_wait_is_persisted_and_early_worker_does_no_http(tmp_path):
    now = [1000.0]
    calls = []
    rate_path = tmp_path / "rate.sqlite"
    normal = nomis_handler(calls)
    throttled = [False]

    def handler(request):
        if request.url.params.get("recordoffset") == "2" and not throttled[0]:
            throttled[0] = True
            calls.append(("429", "2"))
            return httpx.Response(429, headers={"Retry-After": "120"})
        return normal(request)

    def clients(**kwargs):
        limiter = RateLimiter(
            rate_path, interval_seconds=0, max_wait_seconds=0, clock=lambda: now[0]
        )
        return HttpClient(
            transport=httpx.MockTransport(handler), retries=0, rate_limiter=limiter, **kwargs
        )

    service = RetrievalService(tmp_path / "store", clock=lambda: now[0], client_factory=clients)
    _, job = make_job(service, {"nomis": nomis_entry()})
    waiting = service.run_job(job["job_id"])
    assert waiting["status"] == "waiting", waiting
    assert waiting["retry_at"] == 1120
    assert waiting["requests"] == 3
    assert (
        RetrievalService(service.store, clock=lambda: now[0], client_factory=clients).run_job(
            job["job_id"]
        )["status"]
        == "waiting"
    )
    assert len(calls) == 3
    now[0] = 1120
    resumed = RetrievalService(service.store, clock=lambda: now[0], client_factory=clients).run_job(
        job["job_id"]
    )
    assert resumed["status"] == "succeeded", resumed
    assert resumed["requests"] == 5
    assert resumed["entries"]["nomis"]["attempts"] == 2


@pytest.mark.parametrize("budget", [{"max_requests": 2}, {"max_total_bytes": 10}])
def test_cumulative_budgets_cannot_reset_on_restart_or_retry(tmp_path, budget):
    calls = []
    service = RetrievalService(tmp_path, client_factory=factory(nomis_handler(calls)))
    _, job = make_job(service, {"nomis": nomis_entry()}, budget=budget)
    failed = service.run_job(job["job_id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == "budget_exhausted"
    assert not service.store.list()
    assert service.run_job(job["job_id"]) == failed
    with pytest.raises(ValueError, match="budget exhausted"):
        service.retry_job(job["job_id"])
    assert len(calls) <= 2


def test_cancel_running_is_cooperative_and_does_not_publish_lock(tmp_path):
    service = RetrievalService(tmp_path)
    _, job = make_job(service, {"nomis": nomis_entry()})

    def handler(request):
        service.cancel_job(job["job_id"])
        return httpx.Response(200, json=definition())

    service.client_factory = factory(handler)
    cancelled = service.run_job(job["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] and cancelled["requests"] == 1
    assert not Collections(service.store).list()
    assert service.cancel_job(job["job_id"])["status"] == "cancelled"


def test_cancel_queued_and_idle_worker(tmp_path, no_http):
    service = RetrievalService(tmp_path / "store")
    _, job = make_job(service, {"notes": local_entry(service.store, tmp_path)})
    assert service.cancel_job(job["job_id"])["status"] == "cancelled"
    assert service.run_job(job["job_id"])["status"] == "cancelled"
    assert service.run_job() == {"status": "idle"}


def test_cancel_racing_failure_wins_inside_final_state_transaction(tmp_path, monkeypatch):
    service = RetrievalService(tmp_path, client_factory=factory(lambda _: httpx.Response(400)))
    _, job = make_job(service, {"nomis": nomis_entry()})

    def race(*args):
        service.cancel_job(job["job_id"])

    monkeypatch.setattr(service, "_retry_at", race)
    result = service.run_job(job["job_id"])
    assert result["status"] == "cancelled"
    assert result["cancel_requested"] and result["error"]["code"] == "cancelled"


@pytest.mark.parametrize("national", [False, True])
def test_legacy_raw_cache_plans_offline_without_publishing_and_pins_bytes(
    tmp_path, iod_csv, monkeypatch, national
):
    service = RetrievalService(tmp_path)
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
    )
    with HttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=iod_csv))
    ) as client:
        legacy = service.store._acquire(
            request, hashlib.sha256(canonical(request.to_dict())).hexdigest(), client
        )
    assert legacy["schema_version"] == 1 and len(service.store.list()) == 1
    selected_request = (
        deprivation.national_request()
        if national
        else Request(
            "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E09000001"]}
        )
    )
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    plan, job = make_job(service, {"areas": {"request": selected_request.to_dict()}}, offline=True)
    assert len(service.store.list()) == 1
    assert plan["entries"]["areas"]["legacy_snapshot_id"] == legacy["product_id"]
    assert plan["estimated_requests"] == 0
    monkeypatch.undo()
    changed = iod_csv.replace(b"12.375", b"13.375")
    with HttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=changed))
    ) as client:
        newer = SharedDeprivation(service.store).source(refresh=True, client=client)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    done = service.run_job(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["requests"] == 0
    manifest = service.store.verify(done["entries"]["areas"]["snapshot_id"])
    source = manifest if national else service.store.verify(manifest["parent_snapshot_ids"][0])
    assert source["files"]["raw/0000.bin"] == legacy["files"]["raw/0000.bin"]
    assert source["product_id"] != newer["product_id"]
    key = hashlib.sha256(canonical(deprivation.national_request().to_dict())).hexdigest()
    assert service.store._index()[key] == newer["product_id"]
    assert (
        SharedDeprivation(service.store).source(offline=True)["product_id"] == newer["product_id"]
    )


def test_new_lock_preserves_existing_current_collection(tmp_path, no_http):
    service = RetrievalService(tmp_path / "store")
    old_entry = local_entry(service.store, tmp_path, value=1)
    old = Collections(service.store).resolve(
        {"name": "research", "entries": {"notes": old_entry}}, offline=True
    )
    _, job = make_job(
        service, {"notes": local_entry(service.store, tmp_path, value=2)}, offline=True
    )
    done = service.run_job(job["job_id"])
    assert done["status"] == "succeeded"
    assert done["result"]["lock_id"] != old.lock_id
    assert Collections(service.store).get("research").lock_id == old.lock_id
    assert Collections(service.store).replay(old)["entries"]["notes"]["rows"][0]["value"] == "1"
    assert (
        Collections(service.store).replay(done["result"]["lock_id"])["entries"]["notes"]["rows"][0][
            "value"
        ]
        == "2"
    )
    second = service.submit_plan(done["plan_id"], "same-proposal")
    assert service.run_job(second["job_id"])["result"] == done["result"]
    assert Collections(service.store).get("research").lock_id == old.lock_id


def test_local_derivation_uses_planned_national_source(tmp_path, iod_csv, no_http, monkeypatch):
    monkeypatch.undo()
    service = RetrievalService(tmp_path)
    with HttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=iod_csv))
    ) as client:
        source = SharedDeprivation(service.store).source(client=client)
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044", "E09000001"]}
    ).to_dict()
    entry = {"request": request}
    plan, job = make_job(service, {"deprivation": entry}, offline=True)
    assert plan["entries"]["deprivation"]["action"] == "derive"
    assert plan["entries"]["deprivation"]["known_rows"] == 3
    changed = iod_csv.replace(b"12.375", b"13.375")
    with HttpClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=changed))
    ) as client:
        SharedDeprivation(service.store).source(client=client, refresh=True)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    done = service.run_job(job["job_id"])
    assert done["status"] == "succeeded", done
    selected = service.store.verify(done["entries"]["deprivation"]["snapshot_id"])
    assert selected["parent_snapshot_ids"] == [source["product_id"]]
    assert set(service.store.replay(selected["product_id"])["local_authority_code"]) == {
        "E06000044",
        "E09000001",
    }


def test_checkpoint_corruption_fails_without_network_repair(tmp_path):
    calls = []
    normal = nomis_handler(calls)

    def handler(request):
        if request.url.params.get("recordoffset") == "2":
            raise KeyboardInterrupt
        return normal(request)

    service = RetrievalService(tmp_path, client_factory=factory(handler))
    _, job = make_job(service, {"nomis": nomis_entry()})
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job["job_id"])
    next((service.root / "checkpoints").rglob("*.bin")).write_bytes(b"corrupt")
    before = len(calls)
    assert service.run_job(job["job_id"])["status"] == "failed"
    assert len(calls) == before


def test_statswales_resume_reuses_query_creation_and_first_page(tmp_path, source_fixture):
    from test_statswales import DATASET, FILTER_ID, REVISION

    calls = []
    failed = [False]

    def handler(request):
        calls.append((request.method, request.url.path, request.url.params.get("page_number")))
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(200, json={"filterId": FILTER_ID})
        if path.endswith("/filters"):
            return httpx.Response(200, json=source_fixture["dimensions"])
        if "/query/" in path:
            return httpx.Response(
                200,
                json={
                    "id": FILTER_ID,
                    "datasetId": DATASET,
                    "revisionId": REVISION,
                    "totalLines": 3,
                },
            )
        if "/data/" in path:
            number = int(request.url.params["page_number"])
            if number == 2 and not failed[0]:
                failed[0] = True
                raise httpx.ReadTimeout("fixture disconnect", request=request)
            return httpx.Response(200, json=source_fixture["rows"][(number - 1) * 2 : number * 2])
        return httpx.Response(200, json=source_fixture["metadata"])

    service = RetrievalService(tmp_path, client_factory=factory(handler))
    entry = {
        "request": {
            "provider": "statswales",
            "dataset_id": DATASET,
            "params": {"filters": [{"AreaID": ["001", "002", "003"]}]},
            "page_size": 2,
            "max_rows": 10,
        }
    }
    _, job = make_job(service, {"poverty": entry}, budget={"retry_delay_seconds": 0.0})
    waiting = service.run_job(job["job_id"])
    assert waiting["status"] == "waiting", waiting
    assert waiting["requests"] == 6
    done = RetrievalService(tmp_path, client_factory=factory(handler)).run_job(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["requests"] == 9
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert sum(number == "1" for _, _, number in calls) == 1
    rows = Collections(service.store).replay(done["result"]["lock_id"])["entries"]["poverty"][
        "rows"
    ]
    assert len(rows) == 3


def test_revision_change_after_interruption_requires_clean_retry(tmp_path):
    calls = []
    revision = ["before"]
    interrupted = [False]

    def handler(request):
        calls.append((request.url.path, request.url.params.get("recordoffset")))
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=definition(revision[0]))
        offset = int(request.url.params["recordoffset"])
        if offset == 2 and not interrupted[0]:
            interrupted[0] = True
            raise KeyboardInterrupt
        rows = [(0, "12", "A"), (1, "..", "S")] if offset == 0 else [(2, "0", "A")]
        return httpx.Response(200, text=page(rows))

    service = RetrievalService(tmp_path, client_factory=factory(handler))
    _, job = make_job(service, {"nomis": nomis_entry()})
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job["job_id"])
    revision[0] = "after"
    failed = service.run_job(job["job_id"])
    assert failed["status"] == "failed"
    assert not service.store.list()
    assert not Collections(service.store).list()
    queued = service.retry_job(job["job_id"])
    assert queued["entries"]["nomis"]["epoch"] == 1
    done = service.run_job(job["job_id"])
    assert done["status"] == "succeeded", done
    assert done["entries"]["nomis"]["attempts"] == 2
    assert sum(offset == "0" for _, offset in calls) == 2


def crash_worker(root, job_id, ready):
    normal = nomis_handler([])

    def handler(request):
        if request.url.params.get("recordoffset") == "2":
            ready.set()
            signal.pause()
        return normal(request)

    RetrievalService(root, client_factory=factory(handler)).run_job(job_id)


def test_killed_worker_recovers_os_lock_and_verified_http_prefix(tmp_path):
    service = RetrievalService(tmp_path)
    _, job = make_job(service, {"nomis": nomis_entry()})
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=crash_worker, args=(str(tmp_path), job["job_id"], ready))
    worker.start()
    try:
        assert ready.wait(10)
        assert service.get_job(job["job_id"])["status"] == "running"
        # A competing worker cannot acquire the same job while the owner lives.
        assert service.run_job(job["job_id"])["status"] == "running"
        os.kill(worker.pid, signal.SIGKILL)
        worker.join(5)
        assert not worker.is_alive()
        calls = []
        recovered = RetrievalService(
            tmp_path, client_factory=factory(nomis_handler(calls))
        ).run_job(job["job_id"])
        assert recovered["status"] == "succeeded", recovered
        assert recovered["requests"] == 5
        assert len(calls) == 2
        assert calls[0][1] == "2"
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(5)
