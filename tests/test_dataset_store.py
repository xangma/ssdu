"""Actual SDK persistence, historical migration and transfer outcomes."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_nomis import definition, page

from ssdu import Dataset, Request, Store, get_source_coverage
from ssdu.adapters.nomis import default_request, fetch
from ssdu.cli import main
from ssdu.models import RetrievalError
from ssdu.store import canonical


def provider_client():
    def response(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=definition())
        return httpx.Response(200, text=page([(0, "001", "A"), (1, "..", "S"), (2, "0", "A")]))

    from ssdu.http import HttpClient

    return HttpClient(transport=httpx.MockTransport(response))


def historical_snapshot(store):
    request = default_request()
    with provider_client() as client:
        acquisition = fetch(request, client)
        acquisition.dataset = None
        manifest = store._publish(
            request,
            hashlib.sha256(canonical(request.to_dict())).hexdigest(),
            acquisition,
            client.resources,
        )
    return manifest


def file_hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_public_sdk_fetch_and_open_common_dataset(tmp_path):
    store = Store(tmp_path)
    with provider_client() as client:
        dataset = store.fetch_dataset(default_request(), client=client)
        calls = len(client.resources)
        again = store.fetch_dataset(default_request(), offline=True, client=client)
    assert isinstance(dataset, Dataset)
    assert dataset.snapshot_id == again.snapshot_id
    assert len(client.resources) == calls
    assert dataset.contract["contract_version"] == "1.0.0"
    assert dataset.to_arrow()["value_raw"].to_pylist() == ["001", "..", "0"]
    assert dataset.to_arrow()["value"].to_pylist() == [1.0, None, 0.0]
    opened = Store(tmp_path).open_dataset(dataset.snapshot_id)
    assert store.open_dataset(dataset.snapshot_id[:12]).snapshot_id == dataset.snapshot_id
    assert opened.contract == dataset.contract
    assert opened.to_arrow().equals(dataset.to_arrow())
    assert opened.to_pandas("source")["OBS_VALUE"].tolist() == ["001", "..", "0"]
    assert Request is type(default_request())
    assert get_source_coverage()["dataset_contract_version"] == "1.0.0"


def test_historical_migration_is_offline_idempotent_and_preserves_source(tmp_path, monkeypatch):
    store = Store(tmp_path)
    prior = historical_snapshot(store)
    original_path = store.path(prior["product_id"])
    before = file_hashes(original_path)
    index_before = (tmp_path / "latest.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Historical migration attempted provider access")

    monkeypatch.setattr("ssdu.http.HttpClient.__init__", forbidden)
    from ssdu import normalization

    original_normalize = normalization.normalize
    normalizations = []

    def counted(*args, **kwargs):
        normalizations.append(True)
        return original_normalize(*args, **kwargs)

    monkeypatch.setattr(normalization, "normalize", counted)
    with pytest.raises(RetrievalError, match="migrate_dataset"):
        store.open_dataset(prior["product_id"])
    dataset = store.migrate_dataset(prior["product_id"][:12])
    assert dataset.snapshot_id != prior["product_id"]
    assert dataset.descriptor.source.parent_snapshot_ids == [prior["product_id"]]
    assert store.verify(dataset.snapshot_id)["kind"] == "normalization"
    assert store.migrate_dataset(prior["product_id"]).snapshot_id == dataset.snapshot_id
    assert store.fetch_dataset(default_request(), offline=True).snapshot_id == dataset.snapshot_id
    assert len(normalizations) == 1
    (tmp_path / "normalizations.json").unlink()
    assert store.migrate_dataset(prior["product_id"]).snapshot_id == dataset.snapshot_id
    assert len(normalizations) == 1
    assert file_hashes(original_path) == before
    assert (tmp_path / "latest.json").read_bytes() == index_before
    (tmp_path / "latest.json").unlink()
    assert store.fetch(default_request(), offline=True)["product_id"] == prior["product_id"]


def test_normalization_cache_corruption_fails_without_retransform(tmp_path, monkeypatch):
    store = Store(tmp_path)
    prior = historical_snapshot(store)
    dataset = store.migrate_dataset(prior["product_id"])
    (store.path(dataset.snapshot_id) / "tables/observations.parquet").write_bytes(b"damaged")

    def forbidden(*args, **kwargs):
        pytest.fail("Damaged normalization must not be silently recomputed")

    monkeypatch.setattr("ssdu.normalization.normalize", forbidden)
    with pytest.raises(RetrievalError, match="hash mismatch"):
        store.migrate_dataset(prior["product_id"])


def test_normalization_index_cannot_substitute_another_snapshot(tmp_path):
    store = Store(tmp_path)
    prior = historical_snapshot(store)
    store.migrate_dataset(prior["product_id"])
    path = tmp_path / "normalizations.json"
    index = json.loads(path.read_bytes())
    path.write_bytes(canonical(dict.fromkeys(index, prior["product_id"])))
    with pytest.raises(RetrievalError, match="different source or transform"):
        store.migrate_dataset(prior["product_id"])


def test_changed_transform_implementation_creates_an_independent_cached_derivation(
    tmp_path, monkeypatch
):
    import ssdu.store as module

    store = Store(tmp_path)
    prior = historical_snapshot(store)
    first = store.migrate_dataset(prior["product_id"])
    original_checksum = module.checksum

    def changed(path):
        return "f" * 64 if path.name == "normalization.py" else original_checksum(path)

    monkeypatch.setattr(module, "checksum", changed)
    second = store.migrate_dataset(prior["product_id"])
    assert first.snapshot_id != second.snapshot_id
    assert first.to_arrow().equals(second.to_arrow())
    assert store.migrate_dataset(prior["product_id"]).snapshot_id == second.snapshot_id
    assert store.open_dataset(first.snapshot_id).snapshot_id == first.snapshot_id


def test_contract_marker_is_bound_to_verified_descriptor(tmp_path):
    store = Store(tmp_path)
    with provider_client() as client:
        dataset = store.fetch_dataset(default_request(), client=client)
    manifest = store.verify(dataset.snapshot_id)
    manifest["dataset_contract"]["version"] = "9.0.0"
    identity = {k: v for k, v in manifest.items() if k != "product_id"}
    forged_id = hashlib.sha256(canonical(identity)).hexdigest()
    old_path = store.path(dataset.snapshot_id)
    forged_path = store.snapshots / forged_id
    old_path.rename(forged_path)
    manifest["product_id"] = forged_id
    (forged_path / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(RetrievalError, match="marker differs"):
        store.open_dataset(forged_id)


def test_rehashed_descriptor_cannot_omit_retained_raw_inputs(tmp_path):
    store = Store(tmp_path)
    with provider_client() as client:
        dataset = store.fetch_dataset(default_request(), client=client)
    manifest = store.verify(dataset.snapshot_id)
    old_path = store.path(dataset.snapshot_id)
    descriptor_path = old_path / "dataset.json"
    descriptor = json.loads(descriptor_path.read_bytes())
    assert descriptor["source"]["raw_inputs"]
    descriptor["source"]["raw_inputs"] = []
    descriptor_path.write_bytes(canonical(descriptor))
    manifest["files"]["dataset.json"] = {
        "sha256": hashlib.sha256(descriptor_path.read_bytes()).hexdigest(),
        "bytes": descriptor_path.stat().st_size,
    }
    forged_id = hashlib.sha256(
        canonical({key: value for key, value in manifest.items() if key != "product_id"})
    ).hexdigest()
    forged_path = store.snapshots / forged_id
    old_path.rename(forged_path)
    manifest["product_id"] = forged_id
    (forged_path / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(RetrievalError, match="raw-input inventory"):
        store.open_dataset(forged_id)


def test_inconsistent_contract_is_rejected_before_snapshot_publication(tmp_path):
    store = Store(tmp_path)
    request = default_request()
    with provider_client() as client:
        acquisition = fetch(request, client)
        acquisition.source_url = "https://example.invalid/different-source"
        with pytest.raises(RetrievalError, match="provenance differs"):
            store._publish(
                request,
                hashlib.sha256(canonical(request.to_dict())).hexdigest(),
                acquisition,
                client.resources,
            )
    assert list(store.snapshots.iterdir()) == []
    assert not (store.root / "latest.json").exists()


def test_normalized_table_corruption_cannot_be_silently_repaired(tmp_path):
    store = Store(tmp_path)
    with provider_client() as client:
        dataset = store.fetch_dataset(default_request(), client=client)
    (store.path(dataset.snapshot_id) / "tables/observations.parquet").write_bytes(b"damaged")
    with pytest.raises(RetrievalError, match="hash mismatch"):
        store.fetch_dataset(default_request(), offline=True)


def test_dataset_cli_and_source_matrix(tmp_path, capsys):
    store = Store(tmp_path)
    prior = historical_snapshot(store)
    assert main(["sources"]) == 0
    assert json.loads(capsys.readouterr().out)["dataset_contract_version"] == "1.0.0"
    assert main(["dataset", "schema"]) == 0
    assert "contract_version" in json.loads(capsys.readouterr().out)["properties"]
    argv = ["--store", str(tmp_path), "dataset", "migrate", prior["product_id"], "--preview", "2"]
    assert main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["preview"]) == 2
    assert output["dataset"]["source"]["parent_snapshot_ids"] == [prior["product_id"]]
    assert main(["--store", str(tmp_path), "dataset", "show", output["snapshot_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == output["snapshot_id"]
    request_path = Path(tmp_path) / "request.json"
    request_path.write_text(json.dumps(default_request().to_dict()))
    assert main(["--store", str(tmp_path), "dataset", "fetch", str(request_path), "--offline"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == output["snapshot_id"]
    assert (
        main(
            ["--store", str(tmp_path), "dataset", "show", output["snapshot_id"], "--preview", "101"]
        )
        == 1
    )
