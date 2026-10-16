"""Whole captures use invented responses, durable bounds and verified offline replay."""

import copy
import hashlib
import json
import shutil
import socket
import zipfile
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.metadata_capture import MetadataCapture
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_store import MetadataStore
from ssdu.models import RetrievalError
from ssdu.store import canonical
from tests.test_metadata_sync import FIXTURE, sync_for, two_dataset_handler


def capture_for(root, handler=None, clock=None):
    sync, calls, clock = sync_for(root, handler, clock)
    return MetadataCapture(root, clock=clock, client_factory=sync.client_factory), calls, clock


def configuration(**options):
    return {"providers": {"nomis": {"detail": "dataset", **options}}}


def submit(capture, spec=None, key="capture", **options):
    plan = capture.plan_capture(spec or configuration(), **options)
    return capture.submit_plan(plan["plan_id"], key)["job_id"]


def finish(capture, job_id):
    result = capture.run_job(job_id)
    assert result["status"] == "succeeded", result
    return result, capture.report(result["result"]["capture_id"])


def test_complete_capture_resumes_bounded_batches_without_repeating_inventory(tmp_path):
    handler, identities = two_dataset_handler("nomis")
    capture, calls, clock = capture_for(tmp_path, handler)
    job_id = submit(capture, batch_size=1)
    assert not calls
    inventory = capture.run_job(job_id, max_batches=1)
    assert inventory["status"] == "queued" and len(inventory["children"]) == 1
    assert inventory["requests"] == len(calls) == 1
    first_child = inventory["children"][0]["job_id"]
    resumed, resumed_calls, _ = capture_for(tmp_path, handler, clock)
    progress = resumed.run_job(job_id, max_batches=1)
    assert progress["status"] == "queued" and len(progress["children"]) == 2
    assert progress["children"][0]["job_id"] == first_child
    result, manifest = finish(resumed, job_id)
    assert result["requests"] == len(calls) + len(resumed_calls) == 3
    assert result["result"]["snapshots"] == 3
    report = manifest["report"]["providers"]["nomis"]
    assert report["catalogue_records"] == report["described_records"] == len(identities)
    assert report["status"] == "complete" and report["catalogue_exhausted"]
    assert report["inventory_authoritative"] is False
    assert report["all_provider_holdings_percent"] is None
    assert not report["codes_requested"] and not report["codes_complete"]
    assert all(sum(statuses.values()) == 2 for statuses in report["field_status_counts"].values())
    assert report["raw_metadata_bytes"] > 0 and report["normalized_snapshot_bytes"] > 0
    assert MetadataIndex(tmp_path).search("Invented")["total"] == 2
    assert resumed.run_job(job_id) == result


@pytest.mark.parametrize(
    "spec,extra",
    [
        ({"providers": {}}, {}),
        (configuration(dataset_ids=["NM_999_2"]), {}),
        (configuration(detail="catalogue"), {}),
        (configuration(), {"offline": True, "refresh": True}),
        (configuration(), {"batch_size": True}),
        (configuration(), {"batch_size": 101}),
        (configuration(), {"budget": {"max_requests": 10001}}),
    ],
)
def test_strict_local_plans_never_contact_providers(spec, extra, tmp_path):
    capture, calls, _ = capture_for(tmp_path)
    with pytest.raises(ValueError):
        capture.plan_capture(spec, **extra)
    assert calls == [] and capture.list_jobs()["total"] == 0


def test_plan_idempotency_and_cancel_before_work(tmp_path):
    capture, calls, _ = capture_for(tmp_path)
    plan = capture.plan_capture(configuration())
    assert capture.get_plan(plan["plan_id"]) == plan
    assert plan["input"]["budget"]["max_requests"] == 10000
    job = capture.submit_plan(plan["plan_id"], "one")
    assert capture.submit_plan(plan["plan_id"], "one")["job_id"] == job["job_id"]
    other = capture.plan_capture(configuration(query="rural"))
    with pytest.raises(ValueError, match="another capture plan"):
        capture.submit_plan(other["plan_id"], "one")
    assert capture.cancel_job(job["job_id"])["status"] == "cancelled"
    assert capture.run_job(job["job_id"])["status"] == "cancelled"
    assert capture.run_job() == {"status": "idle"}
    with pytest.raises(ValueError, match="Only failed"):
        capture.retry_job(job["job_id"])
    assert not calls


def test_catalogue_bounds_do_not_turn_completed_batches_into_full_coverage(tmp_path):
    handler, _ = two_dataset_handler("nomis")
    capture, _, _ = capture_for(tmp_path, handler)
    result, manifest = finish(capture, submit(capture, configuration(max_datasets=1)))
    report = manifest["report"]["providers"]["nomis"]
    assert result["result"]["completeness"] == report["status"] == "partial"
    assert report["catalogue_records"] == report["described_records"] == 1
    assert report["reported_total"] == 2 and report["catalogue_coverage_percent"] == 50
    assert not report["catalogue_complete"] and report["descriptions_complete"]
    assert not report["inventory_authoritative"]


def test_global_request_budget_counts_completed_children_and_cannot_reset_on_retry(tmp_path):
    handler, _ = two_dataset_handler("nomis")
    capture, calls, _ = capture_for(tmp_path, handler)
    job_id = submit(capture, batch_size=1, budget={"max_requests": 2})
    result = capture.run_job(job_id)
    assert result["status"] == "failed"
    assert result["requests"] == len(calls) == 2
    assert len(result["children"]) == MetadataStore(tmp_path).list()["total"] == 2
    assert all(item["status"] == "succeeded" for item in result["children"])
    assert not list(capture.captures.iterdir())
    with pytest.raises(ValueError, match="budget exhausted"):
        capture.retry_job(job_id)
    assert capture.run_job(job_id)["requests"] == 2


def test_interrupt_preserves_child_identity_and_cumulative_attempts(tmp_path):
    stopped = False
    handler, _ = two_dataset_handler("nomis")

    def interrupt(request):
        nonlocal stopped
        if request.url.path.endswith("/NM_999_3/def.sdmx.json") and not stopped:
            stopped = True
            raise KeyboardInterrupt
        return handler(request)

    capture, calls, clock = capture_for(tmp_path, interrupt)
    job_id = submit(capture, batch_size=1)
    with pytest.raises(KeyboardInterrupt):
        capture.run_job(job_id)
    retained = capture.get_job(job_id)
    assert retained["status"] == "queued" and retained["requests"] == 3
    assert [item["status"] for item in retained["children"]] == ["succeeded", "succeeded", "queued"]
    old_children = [item["job_id"] for item in retained["children"]]
    resumed, new_calls, _ = capture_for(tmp_path, interrupt, clock)
    result, _ = finish(resumed, job_id)
    assert result["requests"] == len(calls) + len(new_calls) == 4
    assert [item["job_id"] for item in result["children"]] == old_children
    assert len(new_calls) == 1 and "NM_999_3" in str(new_calls[0].url)


def test_retry_after_is_retained_without_busy_retries_and_cancel_stops_child(tmp_path):
    first = True

    def rate_limited(request):
        nonlocal first
        if first:
            first = False
            return httpx.Response(429, headers={"Retry-After": "60"})
        return FIXTURE.fixture_response(request)

    capture, calls, clock = capture_for(tmp_path, rate_limited)
    job_id = submit(capture)
    waiting = capture.run_job(job_id)
    assert waiting["status"] == "waiting" and waiting["requests"] == len(calls) == 1
    assert waiting["retry_at"] >= clock.now + 60
    assert capture.run_job(job_id)["status"] == "waiting" and len(calls) == 1
    clock.now = waiting["retry_at"]
    result, _ = finish(capture, job_id)
    assert result["requests"] == len(calls) == 3
    other = submit(capture, key="cancelled-wait", refresh=True)
    first = True
    waiting = capture.run_job(other)
    assert waiting["status"] == "waiting"
    cancelled = capture.cancel_job(other)
    assert cancelled["status"] == "cancelled"
    assert capture.sync.get_job(cancelled["active_child"])["status"] == "cancelled"


def test_quarantined_description_keeps_inventory_and_honest_field_denominator(tmp_path):
    handler, identities = two_dataset_handler("nomis")

    def invalid(request):
        if request.url.path.endswith("/NM_999_3/def.sdmx.json"):
            return httpx.Response(200, json={"invalid_invented_descriptor": True})
        return handler(request)

    capture, _, _ = capture_for(tmp_path, invalid)
    result, manifest = finish(capture, submit(capture, batch_size=1))
    report = manifest["report"]["providers"]["nomis"]
    assert report["catalogue_records"] == 2 and report["described_records"] == 1
    assert report["catalogue_complete"] and not report["descriptions_complete"]
    assert report["quarantined_ids"] == report["missing_records"] == [identities[1]]
    assert report["descriptor_coverage_percent"] == 50
    assert report["field_status_denominator"] == 1
    assert all(sum(statuses.values()) == 1 for statuses in report["field_status_counts"].values())
    assert result["result"]["completeness"] == "partial"
    assert MetadataIndex(tmp_path).get("nomis:" + identities[1])["title"]


def test_failed_empty_offline_capture_makes_no_http_and_retains_job(tmp_path):
    capture, calls, _ = capture_for(tmp_path)
    with patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")):
        result = capture.run_job(submit(capture, offline=True))
    assert result["status"] == "failed" and result["requests"] == result["bytes"] == 0
    assert calls == [] and MetadataStore(tmp_path).list()["total"] == 0


def test_portable_capture_replays_exact_records_from_fresh_store_with_network_blocked(tmp_path):
    handler, _ = two_dataset_handler("nomis")
    original = tmp_path / "source"
    capture, _, clock = capture_for(original, handler)
    result, manifest = finish(capture, submit(capture, batch_size=1))
    capture_id = result["result"]["capture_id"]
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    capture.export_capture(capture_id, first)
    capture.export_capture(capture_id, second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        capture.export_capture(capture_id, first)
    fresh = MetadataCapture(tmp_path / "fresh", clock=clock)
    original.rename(tmp_path / "unavailable-original")
    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("Unexpected DNS")),
        patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected socket")),
    ):
        imported = fresh.import_capture(first)
        assert imported["capture_id"] == capture_id
        assert fresh.report(capture_id) == manifest
        replay, replay_manifest = finish(fresh, submit(fresh, batch_size=1, offline=True))
        assert replay["requests"] == replay["bytes"] == 0
        original_report = copy.deepcopy(manifest["report"])
        replay_report = copy.deepcopy(replay_manifest["report"])
        # cache_hit provenance changes JSON size, not metadata or completeness.
        for report in (original_report, replay_report):
            report["providers"]["nomis"].pop("normalized_snapshot_bytes")
        assert replay_report == original_report
        for old, new in zip(manifest["snapshots"], replay_manifest["snapshots"], strict=True):
            old_doc = MetadataStore(tmp_path / "unavailable-original").read(old["snapshot_id"])
            new_doc = fresh.snapshots.read(new["snapshot_id"])
            assert old_doc["records"] == new_doc["records"]
            assert old_doc["providers"] == new_doc["providers"]
        index = MetadataIndex(fresh.store.root, clock=lambda: datetime.fromtimestamp(clock(), UTC))
        before = index.search("Invented")
        index.rebuild()
        assert index.search("Invented") == before


def test_bundle_corruption_and_rehashed_false_report_fail_before_destination_import(tmp_path):
    capture, _, _ = capture_for(tmp_path / "source")
    result, _ = finish(capture, submit(capture))
    bundle = tmp_path / "capture.zip"
    capture.export_capture(result["result"]["capture_id"], bundle)
    with zipfile.ZipFile(bundle) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    nested = next(name for name in content if name.startswith("snapshots/"))
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as archive:
        for name, raw in content.items():
            archive.writestr(name, raw + b"modified" if name == nested else raw)
    fresh = MetadataCapture(tmp_path / "fresh")
    with pytest.raises(RetrievalError, match="declared bounds"):
        fresh.import_capture(corrupt)
    assert fresh.snapshots.list()["total"] == 0
    changed = copy.deepcopy(json.loads(content["manifest.json"]))
    changed["capture"]["report"]["providers"]["nomis"]["catalogue_records"] = 999
    capture_content = {
        key: value for key, value in changed["capture"].items() if key != "capture_id"
    }
    changed["capture"]["capture_id"] = hashlib.sha256(canonical(capture_content)).hexdigest()
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "w") as archive:
        for name, raw in content.items():
            archive.writestr(name, canonical(changed) if name == "manifest.json" else raw)
    with pytest.raises(RetrievalError, match="report differs"):
        fresh.import_capture(forged)
    assert fresh.snapshots.list()["total"] == 0


def test_capture_report_verifies_snapshot_and_manifest_integrity(tmp_path):
    capture, _, _ = capture_for(tmp_path)
    _, manifest = finish(capture, submit(capture))
    path = capture.captures / f"{manifest['capture_id']}.json"
    altered = copy.deepcopy(manifest)
    altered["report"]["records"] += 1
    path.write_text(json.dumps(altered))
    with pytest.raises(RetrievalError, match="hash mismatch"):
        capture.report(manifest["capture_id"])


@pytest.mark.parametrize("alteration", ["query", "detail", "batch"])
def test_rehashed_outer_scope_cannot_misrepresent_immutable_child_capture(alteration, tmp_path):
    capture, _, _ = capture_for(tmp_path / "source")
    result, _ = finish(capture, submit(capture, configuration(query="Invented")))
    bundle = tmp_path / "capture.zip"
    capture.export_capture(result["result"]["capture_id"], bundle)
    with zipfile.ZipFile(bundle) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    document = json.loads(content["manifest.json"])
    manifest = document["capture"]
    if alteration == "batch":
        manifest["snapshots"][1]["batch"] = 100
    else:
        value = "" if alteration == "query" else "codes"
        manifest["input"]["spec"]["providers"]["nomis"][alteration] = value
        manifest["report"]["providers"]["nomis"]["scope"][alteration] = value
    identity = {key: value for key, value in manifest.items() if key != "capture_id"}
    manifest["capture_id"] = hashlib.sha256(canonical(identity)).hexdigest()
    forged = tmp_path / "scope-forged.zip"
    with zipfile.ZipFile(forged, "w") as archive:
        for name, raw in content.items():
            archive.writestr(name, canonical(document) if name == "manifest.json" else raw)
    fresh = MetadataCapture(tmp_path / "fresh")
    with pytest.raises(RetrievalError, match="scope differs|batch inventory differs"):
        fresh.import_capture(forged)
    assert fresh.snapshots.list()["total"] == 0
    assert not list(fresh.captures.iterdir())


def test_cancellation_after_finalization_boundary_is_not_accepted(tmp_path, monkeypatch):
    capture, _, _ = capture_for(tmp_path)
    job_id = submit(capture)
    publish = capture._publish
    observed = []

    def cancel_at_publication(job, plan):
        observed.append(capture.cancel_job(job["job_id"]))
        return publish(job, plan)

    monkeypatch.setattr(capture, "_publish", cancel_at_publication)
    result, manifest = finish(capture, job_id)
    assert observed[0]["phase"] == "finalizing"
    assert observed[0]["cancel_requested"] is False
    assert result["cancel_requested"] is False
    assert result["result"]["capture_id"] == manifest["capture_id"]


def test_cancellation_before_finalization_publishes_no_complete_capture(tmp_path, monkeypatch):
    capture, _, _ = capture_for(tmp_path)
    job_id = submit(capture)
    task = capture._task

    def cancel_before_commit(job, plan):
        value = task(job, plan)
        if value is None:
            capture.cancel_job(job["job_id"])
        return value

    monkeypatch.setattr(capture, "_task", cancel_before_commit)
    result = capture.run_job(job_id)
    assert result["status"] == "cancelled" and result["cancel_requested"]
    assert result["result"] is None and not list(capture.captures.iterdir())
    assert MetadataStore(tmp_path).list()["total"] == 2


def test_provider_freshness_includes_older_inventory_and_newer_descriptors(tmp_path):
    capture, _, clock = capture_for(tmp_path)
    job_id = submit(capture)
    assert capture.run_job(job_id, max_batches=1)["status"] == "queued"
    inventory_time = datetime.fromtimestamp(clock(), UTC).isoformat()
    clock.now += 100
    _, manifest = finish(capture, job_id)
    report = manifest["report"]["providers"]["nomis"]
    assert report["inventory_last_checked_at"] == report["last_checked_at"] == inventory_time
    assert report["descriptors_last_checked_at"] > inventory_time


def test_empty_inventory_can_have_known_source_freshness(tmp_path):
    def empty_catalogue(request):
        assert request.url.path.endswith("/dataset/def.sdmx.json")
        return httpx.Response(200, json={"structure": {"keyfamilies": {"keyfamily": []}}})

    capture, calls, _ = capture_for(tmp_path, empty_catalogue)
    _, manifest = finish(capture, submit(capture))
    report = manifest["report"]["providers"]["nomis"]
    assert report["catalogue_records"] == report["described_records"] == 0
    assert report["inventory_last_checked_at"] == report["last_checked_at"]
    assert report["last_checked_at"] is not None
    assert report["descriptors_last_checked_at"] is None
    assert len(calls) == 1


def test_unknown_description_check_keeps_combined_freshness_unknown(tmp_path):
    capture, calls, _ = capture_for(tmp_path)
    online = submit(capture)
    assert capture.run_job(online, max_batches=1)["status"] == "queued"
    with patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")):
        _, manifest = finish(capture, submit(capture, offline=True, key="offline-incomplete"))
    report = manifest["report"]["providers"]["nomis"]
    assert report["inventory_last_checked_at"] is not None
    assert report["descriptors_last_checked_at"] is report["last_checked_at"] is None
    assert report["status"] == "partial" and report["described_records"] == 0
    assert len(calls) == 1


def imported_two_batch_capture(tmp_path):
    handler, _ = two_dataset_handler("nomis")
    source, _, clock = capture_for(tmp_path / "source", handler)
    result, _ = finish(source, submit(source, batch_size=1))
    bundle = tmp_path / "capture.zip"
    source.export_capture(result["result"]["capture_id"], bundle)
    fresh = MetadataCapture(tmp_path / "fresh", clock=clock)
    fresh.import_capture(bundle)
    return fresh, clock


def test_offline_capture_recovers_once_per_worker_invocation_and_again_on_resume(tmp_path):
    fresh, clock = imported_two_batch_capture(tmp_path)
    job_id = submit(fresh, batch_size=1, offline=True)
    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")),
        patch.object(fresh.sync, "_seed_snapshots", wraps=fresh.sync._seed_snapshots) as seeding,
    ):
        progress = fresh.run_job(job_id, max_batches=2)
        assert progress["status"] == "queued" and seeding.call_count == 1
        assert fresh.sync._snapshot_recovery.get() is False
    # Removal between worker invocations recovers from verified retained evidence.
    shutil.rmtree(fresh.store.root / "connector-cache")
    # A fresh process-equivalent instance performs another full recovery pass.
    resumed = MetadataCapture(fresh.store.root, clock=clock)
    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")),
        patch.object(
            resumed.sync, "_seed_snapshots", wraps=resumed.sync._seed_snapshots
        ) as seeding,
    ):
        result, _ = finish(resumed, job_id)
        assert result["requests"] == result["bytes"] == 0 and seeding.call_count == 1
        assert resumed.sync._snapshot_recovery.get() is False
        resumed.run_job(job_id)
        assert resumed.run_job() == {"status": "idle"}
        assert seeding.call_count == 1  # terminal and idle reads do no recovery


def test_ordinary_offline_metadata_sync_keeps_independent_recovery_passes(tmp_path):
    fresh, _ = imported_two_batch_capture(tmp_path)
    plan = fresh.sync.plan_sync(configuration(), offline=True)
    first = fresh.sync.submit_plan(plan["plan_id"], "first-sync")
    second = fresh.sync.submit_plan(plan["plan_id"], "second-sync")
    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")),
        patch.object(fresh.sync, "_seed_snapshots", wraps=fresh.sync._seed_snapshots) as seeding,
    ):
        assert fresh.sync.run_job(first["job_id"])["status"] == "succeeded"
        assert fresh.sync.run_job(second["job_id"])["status"] == "succeeded"
        assert seeding.call_count == 2
        assert fresh.sync._snapshot_recovery.get() is False


def test_capture_recovery_context_resets_after_interruption(tmp_path):
    fresh, _ = imported_two_batch_capture(tmp_path)
    job_id = submit(fresh, batch_size=1, offline=True)
    run = fresh.sync.run_job
    calls = 0

    def interrupt_second_child(child_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return run(child_id)

    with (
        patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")),
        patch.object(fresh.sync, "_seed_snapshots", wraps=fresh.sync._seed_snapshots) as seeding,
    ):
        with (
            patch.object(fresh.sync, "run_job", side_effect=interrupt_second_child),
            pytest.raises(KeyboardInterrupt),
        ):
            fresh.run_job(job_id)
        assert seeding.call_count == 1
        assert fresh.sync._snapshot_recovery.get() is False
        assert fresh.get_job(job_id)["status"] == "queued"
        result, _ = finish(fresh, job_id)
        assert result["requests"] == result["bytes"] == 0
        assert seeding.call_count == 2
        assert fresh.sync._snapshot_recovery.get() is False


def test_once_per_capture_recovery_still_rejects_corrupted_cached_response(tmp_path):
    fresh, _ = imported_two_batch_capture(tmp_path)
    fresh.sync._seed_snapshots()
    cache = fresh.store.root / "connector-cache"
    catalogue = next(
        json.loads(path.read_bytes())
        for path in cache.glob("*.json")
        if json.loads(path.read_bytes())["request"]["url"].endswith("/dataset/def.sdmx.json")
    )
    body = cache / "objects" / f"{catalogue['body_sha256']}.bin"
    body.write_bytes(b"invented corruption")
    with patch.object(HttpClient, "request", side_effect=AssertionError("Unexpected HTTP")):
        result = fresh.run_job(submit(fresh, batch_size=1, offline=True))
    assert result["status"] == "failed"
    assert result["requests"] == result["bytes"] == 0
    assert result["result"] is None
    assert fresh.sync._snapshot_recovery.get() is False
