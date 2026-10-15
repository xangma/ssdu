"""Discover invented publications, save selected tables and transfer them offline.

All publications, attachment URLs and values below are independently invented
teaching fixtures. An in-memory HTTP transport supplies every response; no
provider data or network access is used. Run with ``--output-dir NEW_DIRECTORY``.
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

PUBLICATIONS = {
    "synthetic-housing": {
        "title": "Invented housing counts",
        "distribution": "housing-csv",
        "filename": "housing.csv",
        "csv": "area_code,year,dwellings\nA01,2024,12\nA02,2024,9\n",
        "selectors": {"area_code": ["A01"]},
        "expected": [{"area_code": "A01", "year": "2024", "dwellings": "12"}],
    },
    "synthetic-local-finance": {
        "title": "Invented local budgets",
        "distribution": "finance-csv",
        "filename": "finance.csv",
        "csv": "authority,financial_year,budget_gbp\nA01,2024/25,125000\nA02,2024/25,95000\n",
        "selectors": {"authority": ["A02"]},
        "expected": [{"authority": "A02", "financial_year": "2024/25", "budget_gbp": "95000"}],
    },
}


def publication_path(dataset_id):
    return f"/government/statistics/{dataset_id}"


def distribution_url(dataset_id, item):
    return f"https://assets.publishing.service.gov.uk/media/{dataset_id}/{item['filename']}"


def fixture_response(request):
    """Model the documented GOV.UK response shape with wholly invented content."""
    path = request.url.path
    if request.url.host == "www.gov.uk" and path == "/api/search.json":
        results = [
            {"title": item["title"], "link": publication_path(dataset_id)}
            for dataset_id, item in PUBLICATIONS.items()
        ]
        start, count = int(request.url.params["start"]), int(request.url.params["count"])
        return httpx.Response(200, json={"results": results[start : start + count], "total": 2})
    for dataset_id, item in PUBLICATIONS.items():
        if (
            request.url.host == "www.gov.uk"
            and path == f"/api/content{publication_path(dataset_id)}"
        ):
            return httpx.Response(
                200,
                json={
                    "base_path": publication_path(dataset_id),
                    "title": item["title"],
                    "content_id": dataset_id,
                    "public_updated_at": "2026-01-01T00:00:00Z",
                    "description": "Independently invented SSDU teaching table, not statistics.",
                    "links": {
                        "organisations": [
                            {
                                "base_path": "/government/organisations/"
                                "ministry-of-housing-communities-local-government"
                            }
                        ]
                    },
                    "details": {
                        "attachments": [
                            {
                                "id": item["distribution"],
                                "title": item["title"] + " CSV",
                                "url": distribution_url(dataset_id, item),
                                "content_type": "text/csv",
                            }
                        ]
                    },
                },
            )
        if str(request.url) == distribution_url(dataset_id, item):
            return httpx.Response(200, text=item["csv"], headers={"content-type": "text/csv"})
    raise AssertionError(f"Example requested an unexpected fixture: {request.url}")


@contextmanager
def no_network(events):
    """Make accidental use of a real HTTP transport fail, restoring sockets afterwards."""

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
    requests, network_events = [], []

    def transport(request):
        requests.append(str(request.url))
        return fixture_response(request)

    # Zero spacing applies only to this mock transport, not to provider traffic.
    client = HttpClient(
        transport=httpx.MockTransport(transport),
        rate_limiter=RateLimiter(output / "fixture-rate.sqlite3", interval_seconds=0),
    )
    source = Store(output / "source")
    connector = Connector("mhclg", root=source.root, client=client)
    with no_network(network_events), client:
        catalogue = connector.list_datasets(limit=20)
        assert {item["id"] for item in catalogue["items"]} == set(PUBLICATIONS)
        descriptions, snapshots, originals = {}, {}, {}
        for match in catalogue["items"]:
            dataset_id = match["id"]
            item = PUBLICATIONS[dataset_id]
            descriptions[dataset_id] = connector.describe_dataset(dataset_id)
            description = descriptions[dataset_id]
            distribution = next(
                asset for asset in description["distributions"] if asset["supported"]
            )
            request = Request(
                "mhclg",
                dataset_id,
                {
                    "distribution": distribution["id"],
                    "selectors": item["selectors"],
                    "expected_revision": description["release"]["revision"],
                },
            )
            connector.plan_fetch(request)
            snapshots[dataset_id] = connector.fetch(request)
            dataset = connector.fetch_dataset(request, offline=True)
            assert dataset.contract["profile"] == "records"
            originals[dataset_id] = dataset.to_pandas().to_dict(orient="records")
            assert originals[dataset_id] == item["expected"]

        # Retained catalogue and descriptions can be inspected without refetching.
        request_count = len(requests)
        with patch.object(
            HttpClient, "request", side_effect=AssertionError("Offline HTTP attempt")
        ):
            assert connector.list_datasets(limit=20, offline=True) == catalogue
            for dataset_id, description in descriptions.items():
                assert connector.describe_dataset(dataset_id, offline=True) == description
        assert len(requests) == request_count

        collections = Collections(source)
        lock = collections.resolve(
            {
                "name": "invented-publications",
                "entries": {
                    dataset_id: {"snapshot_id": manifest["product_id"]}
                    for dataset_id, manifest in snapshots.items()
                },
            },
            offline=True,
        )
        original_replay = collections.replay(lock.lock_id)
        bundle = output / "invented-publications.ssdu.zip"
        export_bundle(source, lock, bundle)
        source.root.rename(output / "retained-original-store")

        # Only the bundle is supplied to the fresh store. The source path is absent.
        fresh = Store(output / "fresh")
        with patch.object(
            HttpClient, "request", side_effect=AssertionError("Offline HTTP attempt")
        ):
            imported = import_bundle(fresh, bundle)
            assert imported["lock_id"] == lock.lock_id
            assert Collections(fresh).replay(lock.lock_id) == original_replay
            for dataset_id, manifest in snapshots.items():
                dataset = fresh.open_dataset(manifest["product_id"])
                assert dataset.to_pandas().to_dict(orient="records") == originals[dataset_id]
                assert fresh.verify(dataset.snapshot_id) == manifest
        assert len(requests) == request_count

    receipt = {
        "status": "passed",
        "fixture_kind": "independently_invented_teaching_data",
        "provider": "mhclg",
        "datasets": {
            dataset_id: {"snapshot_id": manifest["product_id"], "rows": originals[dataset_id]}
            for dataset_id, manifest in snapshots.items()
        },
        "lock_id": lock.lock_id,
        "fixture_http_requests": len(requests),
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
