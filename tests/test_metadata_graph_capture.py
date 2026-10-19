"""Durable graph workers use only invented catalogue responses."""

import hashlib
import json
import tracemalloc
from types import SimpleNamespace

import httpx
import pytest

from ssdu.http import HttpClient, Resource
from ssdu.metadata_graph import make_entity
from ssdu.metadata_graph_capture import (
    MetadataGraphCapture,
    _canonical,
    _GraphMerge,
    _hash,
    _resource_document,
    _signed,
    _task_id,
)
from ssdu.metadata_graph_store import MetadataGraphStore
from ssdu.rate_limit import RateLimiter


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Backend:
    crash = False
    cycle = False
    conflict = False
    missing = False

    def initial_tasks(self, page_size=50):
        return [{"kind": "inventory", "page_number": 1, "page_size": page_size}]

    def fetch(self, task, client):
        resource = client.get(
            "https://catalogue.example.test/api/" + task["kind"],
            params={"page": task.get("page_number", 0)},
        )
        payload = resource.json()
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt("invented process interruption after retained response")
        provenance = [
            {
                "url": resource.url,
                "body_sha256": hashlib.sha256(resource.body).hexdigest(),
                "pointer": "",
                "captured_at": resource.captured_at,
                "checked_at": resource.checked_at,
            }
        ]
        kind = "dataset" if task["kind"] == "inventory" else "variable"
        dataset = make_entity(
            "synthetic",
            "invented-origin",
            "dataset",
            "survey",
            label="Invented survey",
            provenance=provenance,
        )
        entity = (
            dataset
            if kind == "dataset"
            else make_entity(
                "synthetic",
                "invented-origin",
                "variable",
                "count",
                provenance=provenance,
                description=payload["description"],
            )
        )
        entities, links = [entity], []
        if kind == "variable":
            question = make_entity(
                "synthetic",
                "invented-origin",
                "question",
                "q1",
                provenance=provenance,
                description="How many invented visits?",
            )
            codes = make_entity(
                "synthetic",
                "invented-origin",
                "code_list",
                "answer-codes",
                provenance=provenance,
            )
            code = make_entity(
                "synthetic",
                "invented-origin",
                "code",
                "answer-codes:99",
                provenance=provenance,
                label="Not answered",
                attributes={"value": "99", "missing": True},
            )
            entities += [question, codes, code]
            links = [
                {
                    "source": entity["id"],
                    "target": question["id"],
                    "relation": "measured_by",
                    "provenance": provenance,
                },
                {
                    "source": entity["id"],
                    "target": codes["id"],
                    "relation": "uses_codes",
                    "provenance": provenance,
                },
                {
                    "source": codes["id"],
                    "target": code["id"],
                    "relation": "contains",
                    "provenance": provenance,
                },
                {
                    "source": dataset["id"],
                    "target": entity["id"],
                    "relation": "contains",
                    "provenance": provenance,
                },
            ]
            if self.conflict:
                entities.append({**dataset, "label": "Conflicting invented title"})
        following = (
            []
            if kind == "variable"
            else [
                {
                    "kind": "dictionary",
                    "native_id": "survey",
                    "origin": "invented-origin",
                    "version": None,
                    "page_number": 0,
                    "page_size": 50,
                }
            ]
        )
        if self.cycle:
            following.append(task)
        coverage = [
            {
                "scope": "invented catalogue",
                "kind": kind,
                "status": "exhausted",
                "observed": 1,
                "expected": 1,
                "reason": "Invented fixed test catalogue",
                "provenance": provenance,
            }
        ]
        if self.missing:
            coverage.append(
                {
                    "scope": "invented absent dictionary",
                    "kind": "question",
                    "status": "missing",
                    "observed": 0,
                    "expected": None,
                    "reason": "Not published",
                    "provenance": provenance,
                }
            )
        return {"entities": entities, "links": links, "next_tasks": following, "coverage": coverage}


class Registry:
    def __init__(self, backend):
        self.registration = SimpleNamespace(
            id="synthetic",
            fingerprint="invented-registration-v1",
            catalogue_url="https://catalogue.example.test",
            allowed_hosts=frozenset({"catalogue.example.test"}),
            headers={"X-API-Version": "2.0"},
            cache_scope="metadata:synthetic:2.0",
            backend=lambda: backend,
        )

    def get(self, connector):
        if connector != "synthetic":
            raise ValueError("Unknown synthetic connector")
        return self.registration


def worker(root, *, backend=None, handler=None, clock=None):
    backend, clock, calls = backend or Backend(), clock or Clock(), []

    def respond(request):
        calls.append(request)
        assert request.headers["X-API-Version"] == "2.0"
        if handler:
            return handler(request)
        return httpx.Response(200, json={"description": "Invented visit counts"})

    def factory(**kwargs):
        kwargs["rate_limiter"] = RateLimiter(
            root / "test-rate.sqlite3",
            interval_seconds=0,
            max_wait_seconds=0,
            clock=clock,
        )
        return HttpClient(transport=httpx.MockTransport(respond), **kwargs)

    return (
        MetadataGraphCapture(root, clock=clock, client_factory=factory, registry=Registry(backend)),
        calls,
        clock,
    )


def submit(service, *, key="test", budget=None, offline=False, refresh=False, **spec):
    plan = service.plan_capture(
        {"connector": "synthetic", **spec},
        budget=budget,
        offline=offline,
        refresh=refresh,
    )
    return service.submit_plan(plan["plan_id"], key)["job_id"]


def test_planning_and_submission_are_local_immutable_and_idempotent(tmp_path):
    service, calls, _ = worker(tmp_path)
    plan = service.plan_capture({"connector": "synthetic"})
    assert service.get_plan(plan["plan_id"]) == plan
    job = service.submit_plan(plan["plan_id"], "same")
    assert service.submit_plan(plan["plan_id"], "same")["job_id"] == job["job_id"]
    assert not calls
    other = service.plan_capture({"connector": "synthetic", "page_size": 10})
    with pytest.raises(ValueError, match="another plan"):
        service.submit_plan(other["plan_id"], "same")
    assert service.list_jobs()["total"] == 1
    with service._db() as db:
        tampered = {**plan, "offline": True}
        db.execute("UPDATE plans SET document=?", (json.dumps(tampered),))
    with pytest.raises(Exception, match="checksum"):
        service.get_plan(plan["plan_id"])


@pytest.mark.parametrize(
    "options",
    [
        {"max_pages": 0},
        {"page_size": True},
        {"unknown": 4},
        {"max_entities": 2_000_001},
    ],
)
def test_invalid_plans_do_not_make_requests(tmp_path, options):
    service, calls, _ = worker(tmp_path)
    with pytest.raises(ValueError):
        service.plan_capture({"connector": "synthetic", **options})
    assert calls == []


def test_resume_retains_questions_codes_and_full_cache_reuse(tmp_path):
    service, calls, clock = worker(tmp_path)
    job_id = submit(service)
    yielded = service.run_job(job_id, max_tasks=1)
    assert yielded["status"] == "queued" and yielded["requests"] == 1
    fresh, resumed_calls, _ = worker(tmp_path, clock=clock)
    finished = fresh.run_job(job_id)
    assert finished["status"] == "succeeded", finished
    assert len(calls) == len(resumed_calls) == 1
    snapshot_id = finished["result"]["snapshot_id"]
    rows = fresh.snapshots.entities(snapshot_id, limit=20)["items"]
    assert {row["kind"] for row in rows} == {"dataset", "variable", "question", "code_list", "code"}
    assert next(row for row in rows if row["kind"] == "code")["attributes"]["missing"]
    repeat = fresh.run_job(submit(fresh, key="cached"))
    assert repeat["requests"] == repeat["bytes"] == 0
    assert len(resumed_calls) == 1
    assert fresh.snapshots.entities(repeat["result"]["snapshot_id"], limit=20)["items"] == rows
    assert fresh.report(snapshot_id)["entities"]["instrument"]["unknown"]
    assert not fresh.report(snapshot_id)["complete"]


def test_crash_after_response_uses_positional_checkpoint_even_with_refresh(tmp_path):
    backend = Backend()
    backend.crash = True
    service, calls, clock = worker(tmp_path, backend=backend)
    job_id = submit(service, refresh=True)
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job_id)
    assert len(calls) == 1
    fresh, resumed, _ = worker(tmp_path, clock=clock)
    result = fresh.run_job(job_id)
    assert result["status"] == "succeeded", result
    assert result["requests"] == 2 and len(resumed) == 1


def test_429_wait_is_durable_and_failed_attempt_charged(tmp_path):
    count = 0

    def response(_request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(429, headers={"Retry-After": "120"}, content=b"busy")
        return httpx.Response(200, json={"description": "Invented visit counts"})

    service, calls, clock = worker(tmp_path, handler=response)
    job_id = submit(service)
    waiting = service.run_job(job_id)
    assert waiting["status"] == "waiting" and waiting["requests"] == 1
    assert waiting["bytes"] == len(b"busy")
    assert waiting["retry_at"] == clock.now + 120
    fresh, resumed, _ = worker(tmp_path, clock=clock)
    assert fresh.run_job(job_id)["status"] == "waiting" and resumed == []
    clock.now += 120
    result = fresh.run_job(job_id)
    assert result["status"] == "succeeded" and result["requests"] == 3
    assert len(calls) == 1 and len(resumed) == 2


@pytest.mark.parametrize(
    "configuration,reason",
    [
        ({"budget": {"max_requests": 1}}, "request budget"),
        ({"budget": {"max_parse_bytes": 1}}, "parse budget"),
        ({"budget": {"max_response_bytes": 1}}, "response byte"),
        ({"budget": {"max_total_bytes": 1}}, "download byte"),
        ({"max_tasks": 1}, "task count"),
        ({"max_pages": 1}, "page count"),
        ({"max_entities": 1}, "entity count"),
    ],
)
def test_bounds_produce_partial_not_false_complete(tmp_path, configuration, reason):
    service, calls, _ = worker(tmp_path)
    result = service.run_job(submit(service, **configuration))
    assert result["status"] == "partial", result
    report = result["result"]["report"]
    assert not report["complete"] and any(reason in item for item in report["bounds"])
    assert result["requests"] == len(calls)


def test_attempt_budget_survives_restarts(tmp_path):
    def unavailable(_request):
        return httpx.Response(503, headers={"Retry-After": "10"}, content=b"unavailable")

    service, calls, clock = worker(tmp_path, handler=unavailable)
    job_id = submit(service, budget={"max_attempts_per_task": 2})
    assert service.run_job(job_id)["status"] == "waiting"
    clock.now += 10
    fresh, resumed, _ = worker(tmp_path, handler=unavailable, clock=clock)
    failed = fresh.run_job(job_id)
    assert failed["status"] == "failed", failed
    assert failed["requests"] == 2 and len(calls) == len(resumed) == 1


def test_crashes_during_dispatch_cannot_reset_retry_allowance(tmp_path):
    def interrupted(_request):
        raise KeyboardInterrupt("invented interruption after dispatch reservation")

    service, calls, clock = worker(tmp_path, handler=interrupted)
    job_id = submit(service, budget={"max_attempts_per_task": 2})
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job_id)
    fresh, resumed, _ = worker(tmp_path, handler=interrupted, clock=clock)
    with pytest.raises(KeyboardInterrupt):
        fresh.run_job(job_id)
    final, no_dispatch, _ = worker(tmp_path, clock=clock)
    failed = final.run_job(job_id)
    assert failed["status"] == "failed" and failed["requests"] == 2
    assert len(calls) == len(resumed) == 1 and not no_dispatch


@pytest.mark.parametrize("attribute", ["cycle", "missing", "conflict"])
def test_cycles_missing_metadata_and_conflicts_remain_visible(tmp_path, attribute):
    backend = Backend()
    setattr(backend, attribute, True)
    service, calls, _ = worker(tmp_path, backend=backend)
    result = service.run_job(submit(service))
    assert result["status"] == "partial", result
    report = result["result"]["report"]
    assert not report["complete"] and len(calls) <= 2
    if attribute == "conflict":
        assert "Conflicting" in report["failures"][0]["error"]
    elif attribute == "cycle":
        assert report["warnings"]
    else:
        assert report["entities"]["question"]["missing"]


def test_verified_transfer_replays_offline_without_original_store(tmp_path):
    origin = tmp_path / "origin"
    service, _, _ = worker(origin)
    result = service.run_job(submit(service))
    snapshot_id = result["result"]["snapshot_id"]
    bundle = service.snapshots.export(snapshot_id, tmp_path / "metadata.zip")
    original = service.snapshots.entities(snapshot_id, limit=20)["items"]
    origin.rename(tmp_path / "unavailable-original")
    destination = tmp_path / "fresh"
    store = MetadataGraphStore(destination)
    assert store.import_bundle(bundle) == snapshot_id
    offline, calls, _ = worker(destination)
    offline.client_factory = lambda **_: pytest.fail("Offline workers cannot create a transport")
    replay = offline.run_job(submit(offline, offline=True))
    assert replay["status"] == "succeeded", replay
    assert replay["requests"] == replay["bytes"] == 0 and not calls
    assert (
        offline.snapshots.entities(replay["result"]["snapshot_id"], limit=20)["items"] == original
    )


def test_cancel_and_positional_checkpoint_corruption_are_fail_closed(tmp_path):
    service, calls, _ = worker(tmp_path)
    cancelled = submit(service, key="cancelled")
    service.cancel_job(cancelled)
    assert service.run_job(cancelled)["status"] == "cancelled" and not calls
    backend = Backend()
    backend.crash = True
    service, _, _ = worker(tmp_path, backend=backend)
    job_id = submit(service, key="crash")
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job_id)
    checkpoint = next((service.root / "checkpoints" / job_id).glob("*/request-0.json"))
    document = json.loads(checkpoint.read_bytes())
    body = checkpoint.parent / (document["resource"]["body_sha256"] + ".bin")
    body.write_bytes(b"changed")
    result = service.run_job(job_id)
    assert result["status"] == "failed"
    assert result["error"] or result["result"]["report"]["failure_count"]


def test_cancellation_during_response_still_charges_delivered_bytes(tmp_path):
    body = b'{"description":"invented cancelled response"}'
    service = None
    job_id = None

    def cancel_while_streaming(_request):
        service.cancel_job(job_id)
        return httpx.Response(200, content=body)

    service, calls, _ = worker(tmp_path, handler=cancel_while_streaming)
    job_id = submit(service)
    result = service.run_job(job_id)
    assert result["status"] == "cancelled" and result["requests"] == len(calls) == 1
    assert result["bytes"] == len(body)


def test_json_typed_assertions_cannot_be_silently_merged(tmp_path):
    class TypedConflict(Backend):
        def fetch(self, task, client):
            result = super().fetch(task, client)
            if task["kind"] == "inventory":
                result["entities"][0]["attributes"]["restricted"] = True
            else:
                result["entities"].append(
                    make_entity(
                        "synthetic",
                        "invented-origin",
                        "dataset",
                        "survey",
                        label="Invented survey",
                        provenance=result["entities"][0]["provenance"],
                        attributes={"restricted": 1},
                    )
                )
            return result

    service, _, _ = worker(tmp_path, backend=TypedConflict())
    result = service.run_job(submit(service))
    assert result["status"] == "partial"
    assert result["result"]["report"]["failure_count"] == 1
    rows = service.snapshots.entities(result["result"]["snapshot_id"])["items"]
    assert next(row for row in rows if row["kind"] == "dataset")["attributes"]["restricted"] is True


def test_finalization_streams_resources_in_bounded_memory(tmp_path, monkeypatch):
    service, _, _ = worker(tmp_path)
    job_id = submit(service)
    service._update(job_id, status="running")
    plan = service.get_plan(service.get_job(job_id)["plan_id"])
    registration = service.registry.get("synthetic")
    with service._db() as db:
        db.execute("DELETE FROM tasks WHERE job_id=?", (job_id,))
        for number in range(40):
            task = {"kind": "dictionary", "native_id": str(number), "origin": "invented"}
            root = service.root / "checkpoints" / job_id / _hash(task)
            root.mkdir(parents=True)
            body = _canonical({"invented": number, "padding": "x" * 250_000})
            resource = Resource(
                body,
                f"https://catalogue.example.test/{number}",
                "GET",
                None,
                "application/json",
                {},
                "synthetic",
            )
            document = _resource_document(resource)
            (root / (document["body_sha256"] + ".bin")).write_bytes(body)
            result = {"entities": [], "links": [], "next_tasks": [], "coverage": []}
            receipt = _signed(
                {"task": task, "result": result, "resources": [document]}, "receipt_id"
            )
            (root / "result.json").write_bytes(_canonical(receipt))
            db.execute(
                "INSERT INTO tasks(job_id,task_id,document,status,result) VALUES(?,?,?,?,?)",
                (
                    job_id,
                    _task_id(task),
                    _canonical(task).decode(),
                    "done",
                    _canonical(result).decode(),
                ),
            )

    consumed = 0

    def publish(entities, links, resources, report):
        nonlocal consumed
        assert all(iter(value) is value for value in (entities, links, resources))
        for resource in resources:
            assert len(resource.body) > 250_000
            consumed += 1
        assert list(entities) == list(links) == []
        return "f" * 64

    monkeypatch.setattr(service.snapshots, "publish", publish)
    tracemalloc.start()
    try:
        finished = service._finish(job_id, plan, registration)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert finished["status"] == "succeeded" and consumed == 40
    assert peak < 6_000_000, peak  # The retained raw responses total over 10 MB.
    assert not list(service.root.glob(".graph-merge-*"))


def test_report_samples_bound_bytes_and_missing_lists(tmp_path):
    service, _, _ = worker(tmp_path)
    job_id = submit(service)
    job = service.get_job(job_id)
    with _GraphMerge(service.root, lambda: None) as merged:
        for number in range(1200):
            merged.coverage(
                {
                    "scope": str(number),
                    "kind": "question",
                    "status": "missing",
                    "expected": None,
                    "reason": "Invented missing metadata " + "x" * 5000,
                }
            )
        report = merged.report(job, service.registry.get("synthetic"))
    assert report["coverage_event_count"] == 1200 and report["coverage_truncated"]
    assert report["entities"]["question"]["missing_count"] == 1200
    assert report["entities"]["question"]["missing_truncated"]
    assert len(_canonical(report)) < 1_100_000


def test_resumed_offline_batches_do_not_reseed_entire_catalogue(tmp_path, monkeypatch):
    origin, _, _ = worker(tmp_path / "origin")
    initial = origin.run_job(submit(origin))
    bundle = origin.snapshots.export(initial["result"]["snapshot_id"], tmp_path / "metadata.zip")
    destination = tmp_path / "destination"
    MetadataGraphStore(destination).import_bundle(bundle)
    calls = []
    original_seed = MetadataGraphStore.seed_all_cache

    def seed(store):
        calls.append(store.root)
        return original_seed(store)

    monkeypatch.setattr(MetadataGraphStore, "seed_all_cache", seed)
    offline, _, clock = worker(destination)
    job_id = submit(offline, offline=True)
    assert offline.run_job(job_id, max_tasks=1)["status"] == "queued"
    resumed, _, _ = worker(destination, clock=clock)
    assert resumed.run_job(job_id)["status"] == "succeeded"
    assert len(calls) == 1
