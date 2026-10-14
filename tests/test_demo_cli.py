import hashlib
import json

import pandas as pd
import pytest
from test_deprivation import client_for

from ssdu.adapters import deprivation, nomis, statswales
from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request
from ssdu.store import Store


@pytest.mark.parametrize("saved_profile", [False, True])
@pytest.mark.parametrize("offline", [False, True])
def test_demo_resolves_current_defaults_without_altering_historical_selection(
    tmp_path, iod_csv, monkeypatch, capsys, saved_profile, offline
):
    store = Store(tmp_path / "store")

    def fixture_fetch(request, _):
        return Acquisition(
            request.provider,
            request.dataset_id,
            "Synthetic demo cache fixture",
            pd.DataFrame({"measure": ["fixture"], "value": [1]}),
            {"fixture": True},
            {"edition": "fixture"},
            "Fixture rights",
            "Synthetic fixture; no official observations",
            "https://example.invalid/fixture",
        )

    monkeypatch.setattr(nomis, "fetch", fixture_fetch)
    monkeypatch.setattr(statswales, "fetch", fixture_fetch)
    other = [store.fetch(module.default_request()) for module in (nomis, statswales)]
    selected_request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
    )
    with client_for(iod_csv) as client:
        historical = store.fetch(selected_request, client=client)
    source = store.fetch(deprivation.default_request(), offline=True)
    historical_path = store.path(historical["product_id"])
    before = {
        str(path.relative_to(historical_path)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in historical_path.rglob("*")
        if path.is_file()
    }
    demo_path = store.root / "demo.json"
    if saved_profile:
        demo_path.write_text(
            json.dumps({"product_ids": [m["product_id"] for m in [*other, historical]]})
        )

    def no_http(*args, **kwargs):
        pytest.fail("Demo with retained defaults attempted provider access")

    monkeypatch.setattr(HttpClient, "request", no_http)
    arguments = ["--store", str(store.root), "demo"] + (["--offline"] if offline else [])
    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["product_ids"] == [m["product_id"] for m in [*other, source]]
    assert [product["rows"] for product in result["products"]] == [1, 1, 4]
    assert all(product["intake_roundtrip"] == "passed" for product in result["products"])
    assert result["offline"] is offline
    assert json.loads(demo_path.read_text()) == result
    assert before == {
        str(path.relative_to(historical_path)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in historical_path.rglob("*")
        if path.is_file()
    }
    assert store.verify(historical["product_id"])["rows"] == 2
    assert store.fetch(selected_request, offline=True)["product_id"] == historical["product_id"]
