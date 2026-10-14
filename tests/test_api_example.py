"""Run the real temporary REST and MCP clients against retained national data."""

import importlib.util
import json
import os
from pathlib import Path

import pytest
from test_deprivation import client_for

from ssdu.http import HttpClient
from ssdu.shared import SharedDeprivation
from ssdu.store import Store


@pytest.fixture
def api_example():
    path = Path(__file__).parents[1] / "examples" / "api.py"
    spec = importlib.util.spec_from_file_location("ssdu_api_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def retained_api_store(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path / "store")
    with client_for(iod_csv) as client:
        source = SharedDeprivation(store).source(client=client)

    def no_http(*args, **kwargs):
        pytest.fail("Prepared API example attempted provider HTTP")

    monkeypatch.setattr(HttpClient, "request", no_http)
    return store, source


@pytest.mark.parametrize("decile,expected", [(1, 0), (5, 2)])
def test_api_example_national_queries_allow_empty_results(
    api_example, retained_api_store, capsys, decile, expected
):
    store, source = retained_api_store
    original_request = HttpClient.request
    result = api_example.run(store.root, offline=True, decile=decile)
    assert HttpClient.request is original_request
    assert result["total_rows"] == expected
    assert len(result["rows"]) == expected
    assert result["provenance"]["snapshot_id"] == source["product_id"]
    checks = result["verification"]
    assert checks["scope"] == "england" and checks["local_authority_code"] is None
    assert checks["matching_rows"] == checks["returned_rows"] == expected
    assert checks["source_rows"] == checks["selected_rows"] == 4
    assert checks["rest_mcp_equal"] and checks["external_network_blocked"]
    assert checks["derived_selection"] is None
    validation = store.root / "validation"
    assert json.loads((validation / "api-result.json").read_text()) == result
    assert {path.name for path in validation.iterdir()} == {"api-result.json", "api-rest.log"}
    process = json.loads(capsys.readouterr().out.splitlines()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(process["pid"], 0)


def test_api_example_optional_area_and_local_derivation(api_example, retained_api_store):
    store, source = retained_api_store
    frame = store.replay(source["product_id"])
    code = min(frame["local_authority_code"].unique())
    result = api_example.run(
        store.root, offline=True, local_authority_code=code, decile=1, derive_authority=True
    )
    checks = result["verification"]
    assert checks["scope"] == "local_authority" and checks["local_authority_code"] == code
    assert checks["selected_rows"] == len(frame.loc[frame["local_authority_code"].eq(code)])
    assert checks["derived_selection"]["local_authority_code"] != code
    assert checks["derived_selection"]["provider_requests"] == 0
    assert checks["derived_selection"]["rows"] > 0


def test_api_example_rejects_invalid_selection_before_starting_servers(
    api_example, retained_api_store
):
    store, _ = retained_api_store
    with pytest.raises(ValueError, match="Decile"):
        api_example.run(store.root, offline=True, decile=0)
    with pytest.raises(ValueError, match="absent"):
        api_example.run(store.root, offline=True, local_authority_code="not-retained")
    assert not (store.root / "validation").exists()
