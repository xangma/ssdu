"""Discover invented NI and Scottish datasets, then transfer them offline.

Every catalogue entry, code, description and value is independently invented.
The fixtures model the JSON-stat and RDF protocols; they are not provider data.
Run ``python examples/national_connectors.py --output-dir NEW_DIRECTORY``.
"""

from __future__ import annotations

import argparse
import json
import socket
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from ssdu import Connector, Request, Store
from ssdu.bundles import export_bundle, import_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.rate_limit import RateLimiter

QB = "http://purl.org/linked-data/cube#"
DCT = "http://purl.org/dc/terms/"
PERIOD = "http://purl.org/linked-data/sdmx/2009/dimension#refPeriod"
SCOT_BASE = "http://statistics.gov.scot/data/"
SCOT_ID = "ssdu-invented-events"
MEASURE = "https://example.invalid/ssdu/measure/count"
UNIT = "http://purl.org/linked-data/sdmx/2009/attribute#unitMeasure"
REFERENCES = {
    "nisra": "https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful",
    "scotland": "https://guides.statistics.gov.scot/article/22-querying-data-with-sparql",
}


def nisra_cube():
    """Invented JSON-stat cube: two years of fictional workshop attendance."""
    categories = {
        "STATISTIC": ("Statistic", ["COUNT"], {"COUNT": "Invented workshop attendees"}),
        "TLIST(A1)": ("Year", ["2023", "2024"], {"2023": "2023", "2024": "2024"}),
        "NI": ("Area", ["N_SYNTH_NI"], {"N_SYNTH_NI": "Invented NI area"}),
    }
    dimensions = {
        key: {"label": label, "category": {"index": codes, "label": labels}}
        for key, (label, codes, labels) in categories.items()
    }
    dimensions["STATISTIC"]["category"]["unit"] = {"COUNT": {"label": "People"}}
    return {
        "version": "2.0",
        "class": "dataset",
        "label": "Invented workshop attendance; not published statistics",
        "id": list(categories),
        "size": [1, 2, 1],
        "dimension": dimensions,
        "value": [12, 13],
        "updated": "2024-02-01T00:00:00Z",
        "role": {"metric": ["STATISTIC"], "time": ["TLIST(A1)"], "geo": ["NI"]},
        "extension": {
            "matrix": "SYNTH_TS",
            "copyright": {"name": "Independently invented SSDU teaching values"},
        },
    }


def term(value, kind="literal"):
    return {"type": kind, "value": value}


def bindings(variables, rows):
    return {"head": {"vars": variables}, "results": {"bindings": rows}}


def scotland_metadata():
    """Invented RDF structure: annual fictional community events."""
    rows = [
        {"kind": term("dataset"), "property": term(DCT + key, "uri"), "value": term(value)}
        for key, value in {
            "title": "Invented community events; not published statistics",
            "modified": "2024-02-01T00:00:00Z",
            "license": "https://example.invalid/ssdu/synthetic-fixtures",
        }.items()
    ]
    rows.append(
        {
            "kind": term("dataset"),
            "property": term(QB + "structure", "uri"),
            "value": term(SCOT_BASE + "structure/" + SCOT_ID, "uri"),
        }
    )
    for role, prop, label in (
        ("dimension", PERIOD, "Year"),
        ("measure", MEASURE, "Invented events"),
        ("attribute", UNIT, "Unit"),
    ):
        rows.append(
            {
                "kind": term(QB + role),
                "property": term(prop, "uri"),
                "value": term(prop, "uri"),
                "label": term(label),
            }
        )
    return bindings(["kind", "property", "value", "label", "attachment", "required"], rows)


def scotland_observations():
    observation = term(SCOT_BASE + SCOT_ID + "/observation/2024", "uri")
    rows = []
    for prop, value, label in (
        (QB + "dataSet", term(SCOT_BASE + SCOT_ID, "uri"), "Invented events"),
        (PERIOD, term("http://reference.data.gov.uk/id/year/2024", "uri"), "2024"),
        (MEASURE, term("7"), None),
        (UNIT, term("https://example.invalid/ssdu/unit/count", "uri"), "Invented count"),
    ):
        row = {"obs": observation, "property": term(prop, "uri"), "value": value}
        if label:
            row["label"] = term(label)
        rows.append(row)
    return bindings(["obs", "property", "value", "label"], rows)


def fixture_response(request):
    """Closed mock routing; an unexpected request fails immediately."""
    if request.method == "GET" and request.url.host == "ws-data.nisra.gov.uk":
        path = request.url.path
        base = "/public/api.restful/PxStat.Data.Cube_API."
        cube = nisra_cube()
        if path == base + "ReadCollection":
            return httpx.Response(
                200,
                json={
                    "version": "2.0",
                    "class": "collection",
                    "link": {
                        "item": [{"label": cube["label"], "extension": {"matrix": "SYNTH_TS"}}]
                    },
                },
            )
        if path == base + "ReadMetadata/SYNTH_TS/en":
            return httpx.Response(200, json={k: v for k, v in cube.items() if k != "value"})
        if path == base + "ReadDataset/SYNTH_TS/JSON-stat/2.0/en":
            return httpx.Response(200, json=cube)
    if (
        request.method == "GET"
        and request.url.host == "statistics.gov.scot"
        and request.url.path == "/sparql.json"
    ):
        query = request.url.params["query"]
        if "GROUP BY ?dataset" in query:
            return httpx.Response(
                200,
                json=bindings(
                    ["dataset", "title"],
                    [
                        {
                            "dataset": term(SCOT_BASE + SCOT_ID, "uri"),
                            "title": term("Invented community events; not published statistics"),
                        }
                    ],
                ),
            )
        if "AS ?kind" in query and SCOT_ID in query:
            return httpx.Response(200, json=scotland_metadata())
        if "GROUP BY ?value" in query and SCOT_ID in query:
            return httpx.Response(
                200,
                json=bindings(
                    ["value", "label"],
                    [
                        {
                            "value": term("http://reference.data.gov.uk/id/year/" + year, "uri"),
                            "label": term(year),
                        }
                        for year in ("2023", "2024")
                    ],
                ),
            )
        if "SELECT DISTINCT ?obs ?property" in query and SCOT_ID in query:
            assert "VALUES ?selected0 { <http://reference.data.gov.uk/id/year/2024> }" in query
            return httpx.Response(200, json=scotland_observations())
    raise AssertionError(f"Unexpected fixture request: {request.method} {request.url}")


@contextmanager
def no_network(events):
    def denied(*args, **kwargs):
        events.append("network")
        raise AssertionError("This teaching example permits no network access")

    with ExitStack() as stack:
        for name in ("getaddrinfo", "create_connection"):
            stack.enter_context(patch.object(socket, name, denied))
        for name in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, denied))
        yield


def run(output_dir):
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Choose an absent or empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    calls, network_events = [], []

    def transport(request):
        calls.append(str(request.url))
        return fixture_response(request)

    # Zero spacing is confined to invented responses; live rate policies are untouched.
    client = HttpClient(
        transport=httpx.MockTransport(transport),
        rate_limiter=RateLimiter(output / "fixture-rate.sqlite3", interval_seconds=0),
    )
    source = Store(output / "source")
    connectors = {
        provider: Connector(provider, root=source, client=client)
        for provider in ("nisra", "scotland")
    }
    dimension_ids = {"nisra": "TLIST(A1)", "scotland": PERIOD}
    catalogues, descriptions, codes, requests, datasets = {}, {}, {}, {}, {}
    with no_network(network_events), client:
        for provider, connector in connectors.items():
            catalogues[provider] = connector.list_datasets("invented", limit=20)
            dataset_id = catalogues[provider]["items"][0]["id"]
            descriptions[provider] = connector.describe_dataset(dataset_id)
            dimension = dimension_ids[provider]
            codes[provider] = connector.list_dimension_values(dataset_id, dimension)
            # Native codes remain intact: a NI year is text; a Scottish year is a URI.
            selected = codes[provider]["items"][-1]["value"]
            selection_key = "selectors" if provider == "nisra" else "dimensions"
            requests[provider] = Request(
                provider, dataset_id, {selection_key: {dimension: [selected]}}, max_rows=10
            )
            plan = connector.plan_fetch(requests[provider])
            assert plan["provider_requests_for_values"] == 0
            datasets[provider] = connector.fetch_dataset(requests[provider])
            assert len(datasets[provider].to_pandas()) == 1

        request_count = len(calls)
        with patch.object(
            HttpClient, "request", side_effect=AssertionError("Offline HTTP attempt")
        ):
            for provider, connector in connectors.items():
                dataset_id = requests[provider].dataset_id
                assert connector.list_datasets("invented", offline=True) == catalogues[provider]
                assert (
                    connector.describe_dataset(dataset_id, offline=True) == descriptions[provider]
                )
                assert (
                    connector.list_dimension_values(
                        dataset_id, dimension_ids[provider], offline=True
                    )
                    == codes[provider]
                )
                assert connector.fetch_dataset(requests[provider], offline=True).snapshot_id == (
                    datasets[provider].snapshot_id
                )

        collections = Collections(source)
        lock = collections.resolve(
            {
                "name": "invented-national-connectors",
                "entries": {
                    provider: {"snapshot_id": dataset.snapshot_id}
                    for provider, dataset in datasets.items()
                },
            },
            offline=True,
        )
        replay = collections.replay(lock.lock_id)
        bundle = output / "invented-national-connectors.ssdu.zip"
        export_bundle(source, lock, bundle)
        source.root.rename(output / "retained-original-store")
        fresh = Store(output / "fresh")
        with patch.object(
            HttpClient, "request", side_effect=AssertionError("Offline HTTP attempt")
        ):
            assert import_bundle(fresh, bundle)["lock_id"] == lock.lock_id
            assert Collections(fresh).replay(lock.lock_id) == replay
            for provider, original in datasets.items():
                connector = Connector(provider, root=fresh)
                dataset = connector.fetch_dataset(requests[provider], offline=True)
                assert dataset.snapshot_id == original.snapshot_id
                assert dataset.contract == original.contract
                for name in original.table_names:
                    assert dataset.to_arrow(name).equals(original.to_arrow(name))
                # Acquisition metadata travels with snapshots; unrelated catalogue pages do not.
                assert (
                    connector.describe_dataset(requests[provider].dataset_id, offline=True)
                    == (descriptions[provider])
                )
                fresh.verify(dataset.snapshot_id)
        assert len(calls) == request_count

    receipt = {
        "status": "passed",
        "fixture_kind": "independently_invented_teaching_data",
        "datasets": {
            provider: {
                "snapshot_id": dataset.snapshot_id,
                "request": requests[provider].to_dict(),
                "protocol_reference": REFERENCES[provider],
                "rows": 1,
            }
            for provider, dataset in datasets.items()
        },
        "lock_id": lock.lock_id,
        "fixture_http_requests": len(calls),
        "provider_network_requests": len(network_events),
        "metadata_replayed_offline": True,
        "fresh_store_replayed_offline": True,
        "original_store_path_unavailable": not source.root.exists(),
    }
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
