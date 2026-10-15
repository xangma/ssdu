"""Invented national cubes exercise durable interruption and release-change retry."""

import copy

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.models import Request
from ssdu.retrieval import RetrievalService


@pytest.mark.parametrize("provider", ["nisra", "scotland"])
@pytest.mark.parametrize("change_release", [False, True], ids=["same-release", "changed-release"])
def test_national_job_resumes_prefix_and_clean_retry_resolves_changed_release(
    tmp_path, provider, change_release
):
    if provider == "nisra":
        from test_nisra import load_fixture

        dataset_id = "SYNTH_TS"
        data = load_fixture()
        before = copy.deepcopy(data)
        before.pop("value", None)
        before.pop("status", None)
        after = copy.deepcopy(before)
        after["updated"] = "2026-02-02T00:00:00Z"
        new_data = {**copy.deepcopy(data), "updated": after["updated"]}
        new_data["value"] = [99] * len(data["value"])
    else:
        from test_scotland import fixture

        native, before, data = fixture()
        dataset_id = native["dataset_id"]
        after = copy.deepcopy(before)
        for row in after["results"]["bindings"]:
            if row["property"]["value"].endswith("/modified"):
                row["value"]["value"] = "2026-02-02T00:00:00Z"
        assert after != before
        new_data = copy.deepcopy(data)
        for row in new_data["results"]["bindings"]:
            if "/measure/" in row["property"]["value"]:
                row["value"]["value"] = "99"
    changed, interrupted, calls = [False], [False], []

    def handler(request):
        is_metadata = (
            "ReadMetadata/" in request.url.path
            if provider == "nisra"
            else "AS ?kind" in request.url.params["query"]
        )
        calls.append(("metadata" if is_metadata else "data", changed[0]))
        if is_metadata:
            return httpx.Response(200, json=after if changed[0] else before)
        if not interrupted[0]:
            interrupted[0] = True
            raise KeyboardInterrupt("Invented interruption after metadata checkpoint")
        return httpx.Response(200, json=new_data if changed[0] else data)

    def client_factory(**kwargs):
        return HttpClient(transport=httpx.MockTransport(handler), retries=0, **kwargs)

    service = RetrievalService(tmp_path, client_factory=client_factory)
    plan = service.plan_retrieval(
        {
            "name": "national-resume",
            "entries": {"series": {"request": Request(provider, dataset_id).to_dict()}},
        }
    )
    assert plan["ready"]
    job = service.submit_plan(plan["plan_id"], "national-resume")
    with pytest.raises(KeyboardInterrupt):
        service.run_job(job["job_id"])
    assert service.store.list() == []
    assert calls == [("metadata", False), ("data", False)]
    changed[0] = change_release
    resumed = RetrievalService(tmp_path, client_factory=client_factory).run_job(job["job_id"])
    # The successful metadata prefix is replayed without another provider request.
    assert calls[2] == ("data", change_release)
    if change_release:
        assert resumed["status"] == "failed", resumed
        assert service.store.list() == []
        service.retry_job(job["job_id"])
        resumed = service.run_job(job["job_id"])
    assert resumed["status"] == "succeeded", resumed
    snapshot = resumed["entries"]["series"]["snapshot_id"]
    dataset = service.store.open_dataset(snapshot)
    assert dataset.descriptor.quality.completeness == "complete"
    if change_release:
        assert "99" in dataset.to_arrow()["value_raw"].to_pylist()
    calls_before = list(calls)
    replay = service.store.fetch_dataset(Request(provider, dataset_id), offline=True)
    assert replay.to_arrow().equals(dataset.to_arrow())
    assert calls == calls_before
