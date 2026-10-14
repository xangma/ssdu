"""One trusted registration drives SDK, collections, transport and provenance."""

import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_dataset_store import historical_snapshot

from ssdu.collection_models import RequestSpec
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError
from ssdu.providers import (
    DEFAULT_PROVIDERS,
    ProviderRegistration,
    ProviderRegistry,
    adapter,
    allowed_hosts,
    get_provider,
    provider_ids,
)
from ssdu.store import PROVIDERS, Store


def test_provider_import_is_lazy_and_request_names_cannot_import_modules(monkeypatch):
    script = """
import json, sys
from ssdu.providers import provider_ids
print(json.dumps({'ids': provider_ids(), 'loaded': [name for name in sys.modules
 if name.startswith('ssdu.adapters.') or name.startswith('ssdu.normalization')]}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    document = json.loads(result.stdout)
    assert document["ids"] == list(provider_ids())
    assert document["loaded"] == []

    def forbidden(*args):
        pytest.fail("Unregistered request attempted a dynamic import")

    monkeypatch.setattr("ssdu.providers.importlib.import_module", forbidden)
    for candidate in ("unknown", "../nomis", "ssdu.adapters.nomis", "os", {"id": "nomis"}):
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider(candidate)


def test_registration_rejects_duplicate_names_and_untrusted_module_paths():
    registration = get_provider("nomis")
    with pytest.raises(ValueError, match="Duplicate"):
        ProviderRegistry([registration, registration])
    with pytest.raises(TypeError, match="ProviderRegistration"):
        ProviderRegistry([{"id": "nomis"}])
    for field, value in (
        ("id", "../nomis"),
        ("adapter_module", "os"),
        ("normalizer_module", "os"),
        ("normalizer_name", "__import__"),
        ("transform_version", "latest"),
        ("allowed_hosts", frozenset({"https://example.com"})),
        ("allowed_hosts", frozenset({"example.com:443"})),
        ("allowed_hosts", frozenset({"EXAMPLE.COM"})),
        ("allowed_hosts", frozenset()),
    ):
        with pytest.raises(ValueError):
            replace(registration, **{field: value})


def test_fixed_provider_inventory_and_backwards_compatible_adapter_alias():
    import ssdu.store as store_module

    assert (
        PROVIDERS == provider_ids() == ("nomis", "statswales", "deprivation", "nisra", "scotland")
    )
    assert DEFAULT_PROVIDERS == ("nomis", "statswales", "deprivation")
    assert store_module.adapter is adapter
    assert adapter("nomis").__name__ == "ssdu.adapters.nomis"
    for provider in provider_ids():
        registration = get_provider(provider)
        assert registration.transform_version == "1.0.0"
        paths = registration.implementation_paths()
        assert all(path.is_absolute() for path in paths)
        assert paths[0].name == f"{provider}.py"
        assert {"dataset_contract.py", "providers.py", "normalization.py"} <= {
            path.name for path in registration.normalization_paths()
        }
    assert "normalization_nisra.py" in {
        path.name for path in get_provider("nisra").normalization_paths()
    }
    assert "normalization_scotland.py" in {
        path.name for path in get_provider("scotland").normalization_paths()
    }


def test_transport_host_union_and_explicit_allowlists(tmp_path):
    assert HttpClient.HOSTS == allowed_hosts()
    assert {"ws-data.nisra.gov.uk", "statistics.gov.scot"} <= allowed_hosts()
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"ok": True})

    from ssdu.rate_limit import RateLimiter

    with HttpClient(
        transport=httpx.MockTransport(handler),
        rate_limiter=RateLimiter(tmp_path / "limits.sqlite", interval_seconds=0),
        allowed_hosts=frozenset({"statistics.gov.scot"}),
    ) as client:
        assert client.get("https://statistics.gov.scot/sparql.json").json() == {"ok": True}
        with pytest.raises(RetrievalError, match="allowlist"):
            client.get(
                "https://ws-data.nisra.gov.uk/public/api.restful/PxStat.Data.Cube_API.ReadDataset"
            )
        with pytest.raises(RetrievalError, match="allowlist"):
            client.get("http://statistics.gov.scot/sparql.json")
    assert calls == ["statistics.gov.scot"]


def test_collection_requests_validate_registered_ids_and_expose_schema_enum():
    for provider in provider_ids():
        request = RequestSpec(provider=provider, dataset_id="metadata-is-not-fetched")
        assert request.provider == provider
    with pytest.raises(ValidationError, match="Unknown provider"):
        RequestSpec(provider="untrusted_module", dataset_id="x")
    assert RequestSpec.model_json_schema()["properties"]["provider"]["enum"] == list(provider_ids())


def test_native_validators_are_local_and_new_providers_delegate(monkeypatch):
    from ssdu.adapters.deprivation import national_request
    from ssdu.adapters.nomis import default_request as nomis_default
    from ssdu.adapters.statswales import default_request as wales_default

    for request in (nomis_default(), wales_default(), national_request()):
        get_provider(request.provider).validate_request(request)
    invalid = (
        Request("nomis", "bad", {"geography": "1", "date": "2024"}),
        Request("nomis", "NM_162_1", {"geography": True, "date": "2024"}),
        Request("statswales", "bad"),
        Request("statswales", wales_default().dataset_id, {"filters": [{"YearID": []}]}),
    )
    for request in invalid:
        with pytest.raises((ValueError, RetrievalError)):
            get_provider(request.provider).validate_request(request)
    with pytest.raises(ValueError, match="different provider"):
        get_provider("nisra").validate_request(nomis_default())
    received = []
    monkeypatch.setattr(
        ProviderRegistration,
        "adapter",
        lambda self: SimpleNamespace(validate_request=lambda request: received.append(request)),
    )
    for provider in ("nisra", "scotland"):
        request = Request(provider, "native")
        get_provider(provider).validate_request(request)
        assert received[-1] == request


def test_provider_specific_transform_source_invalidates_migration_cache(tmp_path, monkeypatch):
    extra = tmp_path / "normalization_provider_helper.py"
    extra.write_text("provider interpretation version one\n")
    original = ProviderRegistration.normalization_paths
    monkeypatch.setattr(
        ProviderRegistration, "normalization_paths", lambda self: (*original(self), extra)
    )
    store = Store(tmp_path / "store")
    prior = historical_snapshot(store)
    first = store.migrate_dataset(prior["product_id"])
    assert (
        "normalization_provider_helper.py"
        in store.verify(first.snapshot_id)["implementation_sha256"]
    )
    extra.write_text("provider interpretation version two\n")
    second = store.migrate_dataset(prior["product_id"])
    assert first.snapshot_id != second.snapshot_id
    assert first.to_arrow().equals(second.to_arrow())
    assert store.migrate_dataset(prior["product_id"]).snapshot_id == second.snapshot_id
    assert store.open_dataset(first.snapshot_id).snapshot_id == first.snapshot_id
