"""Opt-in REST controls persist jobs without running acquisition in the transport."""

import pytest
from fastapi.testclient import TestClient
from test_api import FakeService

from ssdu.api import create_app
from ssdu.http import HttpClient
from ssdu.models import RetrievalError

PLAN_ID = "a" * 64
JOB_ID = "job_" + "a" * 32
SPEC = {
    "name": "national-indicators",
    "entries": {"claimants": {"request": {"provider": "nomis", "dataset_id": "NM_1_1"}}},
}
CONTROL_TOOLS = {
    "plan_retrieval",
    "get_plan",
    "submit_plan",
    "get_job",
    "list_jobs",
    "cancel_job",
    "retry_job",
}


class FakeRetrievalService(FakeService):
    def plan_retrieval(self, spec, *, offline=False, refresh=False, budget=None):
        result = {
            "plan_id": PLAN_ID,
            "input": {
                "spec": spec.model_dump(),
                "offline": offline,
                "refresh": refresh,
                "budget": budget.model_dump(),
            },
        }
        self.calls.append(("plan", result))
        return result

    def get_plan(self, plan_id):
        self.calls.append(("get_plan", plan_id))
        return {"plan_id": plan_id}

    def submit_plan(self, plan_id, idempotency_key):
        self.calls.append(("submit", plan_id, idempotency_key))
        return {"job_id": JOB_ID, "plan_id": plan_id, "status": "queued"}

    def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return {"job_id": job_id, "status": "queued"}

    def list_jobs(self, limit=20, offset=0):
        self.calls.append(("list_jobs", limit, offset))
        return {"items": [{"job_id": JOB_ID}], "limit": limit, "offset": offset}

    def cancel_job(self, job_id):
        self.calls.append(("cancel", job_id))
        return {"job_id": job_id, "status": "cancelled"}

    def retry_job(self, job_id):
        self.calls.append(("retry", job_id))
        return {"job_id": job_id, "status": "queued"}


@pytest.fixture
def retrieval_service(monkeypatch):
    def no_http(*args, **kwargs):
        pytest.fail("Retrieval transport attempted provider access")

    monkeypatch.setattr(HttpClient, "request", no_http)
    return FakeRetrievalService()


@pytest.fixture
def retrieval_api(retrieval_service):
    return TestClient(
        create_app(service=retrieval_service, enable_jobs=True), raise_server_exceptions=False
    )


def test_rest_retrieval_controls_require_opt_in(retrieval_service):
    api = TestClient(create_app(service=retrieval_service))
    assert api.get("/health").json()["mode"] == "local-read-only"
    assert api.post("/retrieval/plans", json={"spec": SPEC}).status_code == 404
    assert api.get("/retrieval/jobs").status_code == 404
    assert not any(
        path.startswith("/retrieval/") for path in api.get("/openapi.json").json()["paths"]
    )
    assert retrieval_service.calls == []


def test_rest_plan_and_job_controls(retrieval_api, retrieval_service):
    api = retrieval_api
    assert api.get("/health").json()["mode"] == "local-job-control"
    planned = api.post("/retrieval/plans", json={"spec": SPEC, "offline": True})
    assert planned.status_code == 201
    assert planned.json()["input"]["offline"] is True
    assert retrieval_service.calls[-1] == ("plan", planned.json())
    assert api.get(f"/retrieval/plans/{PLAN_ID}").json() == {"plan_id": PLAN_ID}
    submitted = api.post(
        "/retrieval/jobs", json={"plan_id": PLAN_ID, "idempotency_key": "national-indicators-1"}
    )
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "queued"
    assert retrieval_service.calls[-1] == ("submit", PLAN_ID, "national-indicators-1")
    assert api.get(f"/retrieval/jobs/{JOB_ID}").json()["status"] == "queued"
    assert api.get("/retrieval/jobs?limit=5&offset=2").json()["items"] == [{"job_id": JOB_ID}]
    assert retrieval_service.calls[-1] == ("list_jobs", 5, 2)
    assert api.post(f"/retrieval/jobs/{JOB_ID}/cancel").json()["status"] == "cancelled"
    assert api.post(f"/retrieval/jobs/{JOB_ID}/retry").json()["status"] == "queued"
    schema = api.get("/openapi.json").json()
    for name in ("PlanInput", "SubmitInput", "JobBudget", "CollectionSpec"):
        assert schema["components"]["schemas"][name]["additionalProperties"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"spec": SPEC, "offline": "true"},
        {"spec": SPEC, "offline": True, "refresh": True},
        {"spec": SPEC, "worker": True},
        {"spec": SPEC, "budget": {"max_requests": "10"}},
        {"spec": SPEC, "budget": {"max_requests": 0}},
        {"spec": SPEC, "budget": {"max_total_bytes": 1_000_000_001}},
        {"spec": SPEC, "budget": {"max_attempts": 11}},
        {"spec": SPEC, "budget": {"retry_delay_seconds": 3601}},
        {"spec": SPEC, "budget": {"private_path": "/private/internal/path"}},
    ],
)
def test_rest_rejects_invalid_plans_before_service(retrieval_api, retrieval_service, body):
    response = retrieval_api.post("/retrieval/plans", json=body)
    assert response.status_code == 422
    assert "/private/internal/path" not in response.text
    assert all("input" not in error for error in response.json()["detail"])
    assert retrieval_service.calls == []


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/retrieval/plans/nope", "get", None),
        ("/retrieval/jobs/nope", "get", None),
        ("/retrieval/jobs/nope/cancel", "post", None),
        ("/retrieval/jobs/nope/retry", "post", None),
        ("/retrieval/jobs?limit=101", "get", None),
        ("/retrieval/jobs?offset=-1", "get", None),
        ("/retrieval/jobs?worker=true", "get", None),
        ("/retrieval/jobs", "post", {"plan_id": PLAN_ID, "idempotency_key": ""}),
        ("/retrieval/jobs", "post", {"plan_id": PLAN_ID, "idempotency_key": "a" * 129}),
        (
            "/retrieval/jobs",
            "post",
            {"plan_id": PLAN_ID, "idempotency_key": "safe", "execute": True},
        ),
    ],
)
def test_rest_rejects_invalid_job_arguments(retrieval_api, retrieval_service, path, method, body):
    response = getattr(retrieval_api, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 422
    assert retrieval_service.calls == []


@pytest.mark.parametrize(
    "exception,status",
    [(KeyError, 404), (ValueError, 400), (RetrievalError, 409), (RuntimeError, 500)],
)
def test_rest_control_errors_are_sanitized(retrieval_api, retrieval_service, exception, status):
    def fail(*args, **kwargs):
        raise exception("/private/internal/path")

    retrieval_service.get_job = fail
    result = retrieval_api.get(f"/retrieval/jobs/{JOB_ID}")
    assert result.status_code == status
    assert "/private" not in result.text


def test_rest_bounds_retrieval_input_and_output(retrieval_api, retrieval_service):
    response = retrieval_api.post(
        "/retrieval/plans",
        content=b"x" * 64_001,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    large_spec = {
        "name": "large",
        "entries": {
            "observations": {
                "request": {
                    "provider": "nomis",
                    "dataset_id": "NM_1_1",
                    "params": {"notes": "x" * 60_001},
                }
            }
        },
    }
    bounded = retrieval_api.post("/retrieval/plans", json={"spec": large_spec})
    assert bounded.status_code == 422
    assert "60 KB" in bounded.text
    assert len(bounded.content) < 1000
    assert retrieval_service.calls == []
    retrieval_service.get_job = lambda _: {"notes": "x" * 256_001}
    assert retrieval_api.get(f"/retrieval/jobs/{JOB_ID}").status_code == 400
