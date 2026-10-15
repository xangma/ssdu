"""Shared provider contract: semantic checks use the actual cached acquisition path."""

import importlib.util
from pathlib import Path

import pyarrow as pa
import pytest

from ssdu import Store
from ssdu.http import HttpClient
from ssdu.providers import provider_ids


def example_module():
    path = Path(__file__).resolve().parents[1] / "examples" / "dataset_contract.py"
    spec = importlib.util.spec_from_file_location("ssdu_contract_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEACHING_PROVIDERS = tuple(
    provider
    for provider in provider_ids()
    if provider in example_module().fixture_manifest()["providers"]
)


@pytest.fixture
def example():
    return example_module()


@pytest.mark.parametrize("provider", TEACHING_PROVIDERS)
@pytest.mark.parametrize("page_size", [1, 2, 3])
def test_adapter_common_contract_values_codes_cache_and_provenance(
    provider, page_size, example, tmp_path, monkeypatch
):
    store = Store(tmp_path / "store")
    request = example.request_for(provider, page_size=page_size)
    with example.no_network() as forbidden_connections, example.fixture_client(provider) as client:
        acquire = client.rate_limiter.acquire
        reservations = []

        def reserve(host, **kwargs):
            acquire(host, **kwargs)
            reservations.append(host)

        monkeypatch.setattr(client.rate_limiter, "acquire", reserve)
        dataset = store.fetch_dataset(request, client=client)
        example.check_dataset(dataset, provider)
        assert client.attempts == len(reservations) == len(client.fixture_responses.calls)
        assert client.attempts > 0
        assert forbidden_connections == []
    contract = dataset.contract
    assert contract["contract_version"] == "1.0.0"
    assert contract["profile"] == "observations"
    assert contract["source"]["provider"] == provider
    assert contract["source"]["dataset_id"] == request.dataset_id
    assert contract["source"]["effective_request"] == request.to_dict()
    assert contract["source"]["url"].startswith("https://")
    assert contract["source"]["citation"]
    assert contract["source"]["release"]
    assert contract["source"]["raw_inputs"]
    assert contract["transform"]["id"]
    from ssdu.providers import get_provider

    assert contract["transform"]["version"] == get_provider(provider).transform_version
    assert contract["quality"]["completeness"] == "complete"
    assert all(
        str(dataset.to_arrow().schema.field(dim["column"]).type) == "string"
        for dim in contract["dimensions"]
    )

    def forbidden_http(*args, **kwargs):
        pytest.fail("A cached dataset contacted a provider")

    monkeypatch.setattr(HttpClient, "request", forbidden_http)
    cached = store.fetch_dataset(request)
    offline = store.fetch_dataset(request, offline=True)
    reopened = Store(store.root).open_dataset(dataset.snapshot_id)
    assert cached.snapshot_id == offline.snapshot_id == reopened.snapshot_id == dataset.snapshot_id
    assert reopened.contract == contract
    for name in ("observations", "source"):
        assert reopened.to_arrow(name).equals(dataset.to_arrow(name))


@pytest.mark.parametrize("provider", TEACHING_PROVIDERS)
def test_variable_units_and_non_numeric_meanings_are_explicit(provider, example, tmp_path):
    with example.no_network(), example.fixture_client(provider) as client:
        dataset = Store(tmp_path).fetch_dataset(example.request_for(provider), client=client)
    table = dataset.to_arrow()
    assert pa.types.is_floating(table.schema.field("value").type)
    nulls = [row for row in table.to_pylist() if row["value"] is None]
    assert nulls, "The fixture must exercise unavailable observations"
    assert any(row["value_raw"] not in (None, "") for row in nulls)
    assert any(row["value"] == 0.0 for row in table.to_pylist()) or provider == "statswales"
    variables = {variable["id"]: variable for variable in dataset.contract["variables"]}
    if provider == "deprivation":
        assert variables["imd_rank"]["kind"] == "rank"
        assert variables["imd_decile"]["kind"] == "decile"
        assert variables["imd_score"]["kind"] == "score"
        assert dataset.contract["source"]["release"]["lsoa_geography"] == "2021"
        assert dataset.contract["source"]["release"]["lad_geography"] == "2024"
        # An edition is not an observation period for every deprivation measure.
        assert not any(dim["kind"] == "time" for dim in dataset.contract["dimensions"])
    elif provider == "statswales":
        assert all(variable["unit"] is None for variable in variables.values())
        assert all(value is None for value in table["unit"].to_pylist())
        assert dataset.contract["source"]["licence"] == "Not stated in StatsWales API metadata"
        assert dataset.contract["coverage"]["population"] is None
    else:
        assert table["status"].to_pylist() == ["A", "S", "A"]
        assert "001" in table["value_raw"].to_pylist()


def test_teaching_fixture_references_and_bytes_are_bound(example):
    manifest = example.fixture_manifest()
    assert set(manifest["providers"]) == set(example.PROVIDERS) == set(TEACHING_PROVIDERS)
    assert manifest["kind"] == "synthetic_teaching_fixtures"
    assert "not published statistics" in manifest["description"]
    assert all(item["reference"].startswith("https://") for item in manifest["providers"].values())
    assert {name for item in manifest["providers"].values() for name in item["files"]} == set(
        manifest["files"]
    )


def test_example_network_guard_rejects_real_connection_attempts(example):
    import socket

    with example.no_network() as attempts, pytest.raises(AssertionError, match="Network disabled"):
        socket.create_connection(("example.invalid", 443))
    assert attempts == ["network connection"]


def test_fixture_client_preserves_live_rate_policy(example, tmp_path, monkeypatch):
    live_database = tmp_path / "live-rate.sqlite3"
    monkeypatch.setenv("SSDU_RATE_DB", str(live_database))
    monkeypatch.setenv("SSDU_RATE_INTERVAL_SECONDS", "2.5")
    with example.fixture_client("nomis") as client:
        assert client.rate_limiter.path != live_database
        assert not live_database.exists()
    with HttpClient() as live_client:
        assert live_client.rate_limiter.path == live_database
        assert live_client.rate_limiter.interval_seconds == 2.5
