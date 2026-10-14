import json
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest

from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.store import Store


@pytest.fixture(autouse=True)
def no_provider_requests(monkeypatch):
    monkeypatch.setattr(
        HttpClient, "request", lambda *args, **kwargs: pytest.fail("CLI attempted provider HTTP")
    )


@pytest.fixture
def request_file(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"provider": "nomis", "dataset_id": "fixture"}))
    return path


@pytest.fixture
def fake_store(monkeypatch, tmp_path):
    class FakeStore:
        calls: ClassVar[list] = []
        report: ClassVar[dict] = {"snapshot_count": 1, "invalid_snapshots": [], "index_errors": []}
        failure = None

        def __init__(self, root):
            self.root = Path(root)

        def fetch(self, request, *, refresh=False, offline=False):
            self.calls.append(("fetch", request, refresh, offline))
            if self.failure:
                raise self.failure
            return {
                "product_id": "a" * 64,
                "provider": request.provider,
                "dataset_id": request.dataset_id,
                "title": "Fixture",
                "rows": 1,
                "release": {},
                "created_at": "2026-10-13T00:00:00Z",
            }

        def catalogue(self, product_ids=None, output=None):
            return output or tmp_path / "catalog.yaml"

        def cache_status(self, *, verify=False):
            self.calls.append(("cache_status", verify))
            return self.report

        def rebuild_index(self):
            self.calls.append(("rebuild_index",))
            return self.report

    monkeypatch.setattr("ssdu.cli.Store", FakeStore)
    return FakeStore


@pytest.mark.parametrize(
    "flag,refresh,offline",
    [(None, False, False), ("--offline", False, True), ("--refresh", True, False)],
)
def test_cli_fetch_policy_forwarding(fake_store, request_file, capsys, flag, refresh, offline):
    args = ["fetch", str(request_file)] + ([flag] if flag else [])
    assert main(args) == 0
    call = fake_store.calls[0]
    assert call[0] == "fetch" and call[2:] == (refresh, offline)
    assert json.loads(capsys.readouterr().out)["product_id"] == "a" * 64


def test_cli_fetch_rejects_conflicting_policy_before_read(fake_store, request_file):
    with pytest.raises(SystemExit) as caught:
        main(["fetch", str(request_file), "--offline", "--refresh"])
    assert caught.value.code == 2
    assert fake_store.calls == []


@pytest.mark.parametrize(
    "flags,call",
    [
        ([], ("cache_status", False)),
        (["--verify"], ("cache_status", True)),
        (["--rebuild-index"], ("rebuild_index",)),
        (["--verify", "--rebuild-index"], ("rebuild_index",)),
    ],
)
def test_cli_cache_dispatch_and_json(fake_store, capsys, flags, call):
    assert main(["cache", *flags]) == 0
    assert fake_store.calls == [call]
    assert json.loads(capsys.readouterr().out) == fake_store.report


@pytest.mark.parametrize("flag", ["--verify", "--rebuild-index"])
@pytest.mark.parametrize("field", ["invalid_snapshots", "index_errors"])
def test_cli_cache_unresolved_errors_exit_nonzero(fake_store, capsys, flag, field):
    fake_store.report = {"invalid_snapshots": [], "index_errors": [], field: ["failed"]}
    assert main(["cache", flag]) == 1
    assert json.loads(capsys.readouterr().out)[field] == ["failed"]


def test_cli_repaired_index_is_success(fake_store, capsys):
    fake_store.report = {
        "invalid_snapshots": [],
        "index_errors": [],
        "repaired_index_errors": ["Prior index invalid"],
    }
    assert main(["cache", "--rebuild-index"]) == 0
    assert json.loads(capsys.readouterr().out)["repaired_index_errors"]


def test_cli_retrieval_failure_remains_a_failure(fake_store, request_file, capsys):
    fake_store.failure = RetrievalError("No retained capture; provider access disabled")
    assert main(["fetch", str(request_file), "--offline"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "provider access disabled" in output.err


@pytest.fixture
def retained_capture(tmp_path, monkeypatch):
    class Adapter:
        @staticmethod
        def fetch(request, client):
            return Acquisition(
                "nomis",
                request.dataset_id,
                "Small offline fixture",
                pd.DataFrame({"code": ["001", "002"], "value": [1, 2]}),
                {"definitions": "Fixture only"},
                {"kind": "fixture"},
                "OGL",
                "Fixture citation",
                "https://www.nomisweb.co.uk/fixture",
            )

    monkeypatch.setattr("ssdu.store.adapter", lambda provider: Adapter)
    store = Store(tmp_path / "store")
    request = Request("nomis", "fixture")
    manifest = store.fetch(request)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request.to_dict()))
    return store, manifest, path


def test_actual_cli_offline_cache_and_missing_index_recovery(retained_capture, capsys):
    store, manifest, path = retained_capture
    args = ["--store", str(store.root), "fetch", str(path), "--offline"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["product_id"] == manifest["product_id"]
    (store.root / "latest.json").unlink()
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["product_id"] == manifest["product_id"]


def test_actual_cli_cache_status_verify_and_repair(retained_capture, capsys):
    store, manifest, _ = retained_capture
    args = ["--store", str(store.root), "cache"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["snapshot_count"] == 1 and report["verified_count"] is None
    assert report["request_entries"] == 1
    assert main([*args, "--verify"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified_count"] == 1 and report["invalid_snapshots"] == []
    (store.root / "latest.json").write_text("not JSON")
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["index_errors"]
    assert main([*args, "--rebuild-index"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["index_errors"] == [] and report["request_entries"] == 1
    assert manifest["product_id"] in (store.root / "latest.json").read_text()


def test_actual_cli_corrupt_capture_fails_closed(retained_capture, capsys):
    store, manifest, path = retained_capture
    (store.path(manifest["product_id"]) / "data.parquet").write_bytes(b"corrupt")
    assert main(["--store", str(store.root), "fetch", str(path), "--offline"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "hash mismatch" in output.err
    assert main(["--store", str(store.root), "cache", "--verify"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["invalid_snapshots"][0]["product_id"] == manifest["product_id"]


def test_actual_cli_offline_missing_capture_fails(tmp_path, request_file, capsys):
    assert main(["--store", str(tmp_path / "empty"), "fetch", str(request_file), "--offline"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "ssdu:" in output.err


def test_healthy_fetch_catalogue_ignores_unrelated_damaged_capture(retained_capture, capsys):
    import intake

    store, manifest, path = retained_capture
    damaged = store.snapshots / ("b" * 64)
    damaged.mkdir()
    (damaged / "manifest.json").write_text("broken JSON")
    assert main(["--store", str(store.root), "fetch", str(path), "--offline"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["product_id"] == manifest["product_id"]
    catalog = intake.from_yaml_file(result["catalogue"])
    assert list(catalog.entries) == [f"nomis_{manifest['product_id'][:12]}"]
    assert (damaged / "manifest.json").read_text() == "broken JSON"
