import json

import pytest

from ssdu.cli import main

PLAN = "a" * 64
JOB = "job_" + "b" * 32


class FakeService:
    def __init__(self):
        self.calls = []
        self.results = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "run_job" and self.results:
                return self.results.pop(0)
            return {"operation": name}

        return call


@pytest.fixture
def cli_service(tmp_path, monkeypatch):
    # Collections retains its Service import; load it before replacing the constructor.
    import ssdu.collections  # noqa: F401

    service = FakeService()

    def construct(root, *, index_store):
        assert str(root) == str(tmp_path)
        assert index_store is False
        return service

    monkeypatch.setattr("ssdu.service.Service", construct)
    return service, ["--store", str(tmp_path)]


def test_plan_reads_general_spec_and_forwards_policy_budget(cli_service, tmp_path, capsys):
    service, prefix = cli_service
    spec = {"name": "general", "entries": {"local": {"snapshot_id": "c" * 64}}}
    path = tmp_path / "collection.yaml"
    path.write_text(json.dumps(spec))
    assert (
        main(
            prefix
            + [
                "retrieval",
                "plan",
                str(path),
                "--offline",
                "--max-requests",
                "7",
                "--max-total-bytes",
                "12000",
                "--max-attempts",
                "2",
                "--retry-delay-seconds",
                "0",
            ]
        )
        == 0
    )
    name, args, kwargs = service.calls.pop()
    assert name == "plan_retrieval" and args == (spec,)
    assert kwargs["offline"] is True and kwargs["refresh"] is False
    assert kwargs["budget"].model_dump() == {
        "max_requests": 7,
        "max_total_bytes": 12000,
        "max_attempts": 2,
        "retry_delay_seconds": 0.0,
    }
    assert json.loads(capsys.readouterr().out) == {"operation": "plan_retrieval"}


def test_invalid_budget_never_submits_plan(cli_service, tmp_path, capsys):
    service, prefix = cli_service
    path = tmp_path / "collection.json"
    path.write_text("{}")
    assert main(prefix + ["retrieval", "plan", str(path), "--max-requests", "0"]) == 1
    assert service.calls == []
    assert "max_requests" in capsys.readouterr().err


def test_plan_policy_mutually_exclusive(cli_service, tmp_path):
    _, prefix = cli_service
    with pytest.raises(SystemExit) as caught:
        main(prefix + ["retrieval", "plan", str(tmp_path), "--offline", "--refresh"])
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "arguments,method,args,kwargs",
    [
        (["retrieval", "plan-show", PLAN], "get_plan", (PLAN,), {}),
        (
            ["retrieval", "submit", PLAN, "--idempotency-key", "example-1"],
            "submit_plan",
            (PLAN, "example-1"),
            {},
        ),
        (["jobs", "list"], "list_jobs", (), {"limit": 20, "offset": 0}),
        (
            ["jobs", "list", "--limit", "4", "--offset", "2"],
            "list_jobs",
            (),
            {"limit": 4, "offset": 2},
        ),
        (["jobs", "show", JOB], "get_job", (JOB,), {}),
        (["jobs", "cancel", JOB], "cancel_job", (JOB,), {}),
        (["jobs", "retry", JOB], "retry_job", (JOB,), {}),
        (["jobs", "work"], "run_job", (None,), {}),
        (["jobs", "work", JOB], "run_job", (JOB,), {}),
    ],
)
def test_queue_controls_forward_without_running_implicitly(
    cli_service, capsys, arguments, method, args, kwargs
):
    service, prefix = cli_service
    assert main(prefix + arguments) == 0
    assert service.calls == [(method, args, kwargs)]
    assert json.loads(capsys.readouterr().out) == {"operation": method}


def test_watch_specific_job_stops_at_terminal_state(cli_service, capsys, monkeypatch):
    service, prefix = cli_service
    service.results = [{"status": "waiting"}, {"status": "succeeded", "job_id": JOB}]
    intervals = []
    monkeypatch.setattr("ssdu.retrieval_cli.time.sleep", intervals.append)
    assert main(prefix + ["jobs", "work", JOB, "--watch", "--poll-seconds", "0.5"]) == 0
    assert intervals == [0.5]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"status": "waiting"},
        {"status": "succeeded", "job_id": JOB},
    ]


def test_watch_interrupt_reports_stop_and_returns_130(cli_service, capsys, monkeypatch):
    service, prefix = cli_service
    service.results = [{"status": "idle"}]

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("ssdu.retrieval_cli.time.sleep", interrupt)
    assert main(prefix + ["jobs", "work", "--watch"]) == 130
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"status": "idle"},
        {"status": "worker_stopped", "job_id": None},
    ]


@pytest.mark.parametrize("value", ["0", "-1", "61", "nan", "inf"])
def test_worker_poll_interval_bounded(cli_service, value):
    service, prefix = cli_service
    with pytest.raises(SystemExit) as caught:
        main(prefix + ["jobs", "work", "--poll-seconds", value])
    assert caught.value.code == 2 and service.calls == []


def test_missing_job_is_a_clean_cli_error(cli_service, capsys):
    service, prefix = cli_service

    def missing(_):
        raise KeyError(JOB)

    service.get_job = missing
    assert main(prefix + ["jobs", "show", JOB]) == 1
    assert "Retrieval plan or job not found" in capsys.readouterr().err


@pytest.mark.parametrize("enabled", [False, True])
def test_serve_jobs_require_explicit_opt_in(tmp_path, monkeypatch, enabled):
    calls = []
    app = object()
    monkeypatch.setattr(
        "ssdu.api.create_app", lambda root, **kwargs: calls.append((root, kwargs)) or app
    )
    monkeypatch.setattr("uvicorn.run", lambda value, **kwargs: calls.append((value, kwargs)))
    assert main(["--store", str(tmp_path), "serve"] + (["--enable-jobs"] if enabled else [])) == 0
    assert calls == [
        (tmp_path, {"enable_jobs": True} if enabled else {}),
        (app, {"host": "127.0.0.1", "port": 8000}),
    ]


@pytest.mark.parametrize("enabled", [False, True])
def test_mcp_jobs_require_explicit_opt_in(tmp_path, monkeypatch, enabled):
    calls = []
    monkeypatch.setattr("ssdu.mcp_server.main", calls.append)
    assert main(["--store", str(tmp_path), "mcp"] + (["--enable-jobs"] if enabled else [])) == 0
    assert calls == [
        [
            "--store",
            str(tmp_path),
            "--transport",
            "stdio",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
        ]
        + (["--enable-jobs"] if enabled else [])
    ]


def test_actual_local_collection_plan_job_and_replay_offline(tmp_path, monkeypatch, capsys):
    def no_http(*args, **kwargs):
        pytest.fail("Offline local collection attempted provider access")

    monkeypatch.setattr("ssdu.http.HttpClient.request", no_http)
    table = tmp_path / "survey.csv"
    table.write_text("topic,value\nhousing,10\nhealth,20\n")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "dataset_id": "survey",
                "title": "Local topic survey",
                "column_types": {"value": "int64"},
            }
        )
    )
    prefix = ["--store", str(tmp_path / "store")]

    def command(*arguments):
        assert main(prefix + list(arguments)) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        return json.loads(captured.out)

    snapshot = command("import-local", str(table), "--metadata", str(metadata))["product_id"]
    spec = tmp_path / "general.json"
    spec.write_text(
        json.dumps(
            {
                "name": "topic-survey",
                "entries": {
                    "survey": {
                        "snapshot_id": snapshot,
                        "query": {"filters": [{"column": "value", "value": 20}]},
                    }
                },
            }
        )
    )
    plan = command("retrieval", "plan", str(spec), "--offline")
    assert plan["ready"] is True and plan["provider_access_required"] is False
    assert command("retrieval", "plan-show", plan["plan_id"]) == plan
    job = command("retrieval", "submit", plan["plan_id"], "--idempotency-key", "local-survey")
    job_id = job["job_id"]
    assert job["status"] == "queued"
    assert command("jobs", "show", job_id) == job
    assert (
        command("retrieval", "submit", plan["plan_id"], "--idempotency-key", "local-survey")[
            "job_id"
        ]
        == job_id
    )
    finished = command("jobs", "work", job_id)
    assert finished["status"] == "succeeded", finished
    assert finished["requests"] == 0 and finished["bytes"] == 0
    assert command("jobs", "show", job_id) == finished
    replay = command("collection", "replay", finished["result"]["lock_id"])
    assert replay["entries"]["survey"]["rows"] == [{"topic": "health", "value": 20}]
