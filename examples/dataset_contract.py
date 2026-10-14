"""Executable three-provider contract example using labelled synthetic responses.

Run ``python examples/dataset_contract.py --output-dir OUTPUT``. Every acquisition
uses SSDU's real adapter, HTTP accounting and Store paths with a fixture transport.
No provider or other network connection is permitted. Replace the fixture client
with the default client when retrieving published data; the API stays the same.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import socket
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import pyarrow as pa

from ssdu.adapters import deprivation, statswales
from ssdu.bundles import export_bundle, import_bundle
from ssdu.collections import Collections
from ssdu.http import HttpClient
from ssdu.models import Request
from ssdu.rate_limit import RateLimiter
from ssdu.store import Store, canonical

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dataset-contract"
PROVIDERS = ("nomis", "statswales", "deprivation")


def fixture_manifest() -> dict:
    """Verify all retained response bytes before using them."""
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    if manifest["kind"] != "synthetic_teaching_fixtures":
        raise ValueError("Teaching fixture provenance changed")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Teaching fixture hash mismatch: {name}")
    return manifest


def request_for(provider: str, *, page_size=2) -> Request:
    """Explicit selectors for the tiny teaching responses, never a live default."""
    if provider == "nomis":
        return Request(
            provider,
            "NM_162_1",
            {"geography": "2092957697", "date": "2025-01-2025-03", "measures": "001"},
            page_size=page_size,
            max_rows=10,
        )
    if provider == "statswales":
        return Request(
            provider,
            statswales.DEFAULT_ID,
            {"filters": [{"AreaID": ["001", "002", "003"]}, {"YearID": ["2025"]}]},
            page_size=page_size,
            max_rows=10,
        )
    if provider == "deprivation":
        return deprivation.national_request()
    raise ValueError(f"Unsupported teaching fixture: {provider}")


class FixtureResponses:
    """Closed transport: an unexpected endpoint or selector fails immediately."""

    def __init__(self, provider: str):
        fixture_manifest()
        self.provider = provider
        self.calls: list[dict] = []
        self.sw = json.loads((FIXTURES / "statswales.json").read_bytes())
        self.nomis_rows = list(
            csv.DictReader(io.StringIO((FIXTURES / "nomis-data.csv").read_text()))
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._response(request)
        response.headers["x-ssdu-fixture"] = "synthetic_teaching_fixtures"
        return response

    def _response(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"method": request.method, "url": str(request.url)})
        path = request.url.path
        if self.provider == "nomis" and request.url.host == "www.nomisweb.co.uk":
            if path == "/api/v01/dataset/NM_162_1/def.sdmx.json":
                return httpx.Response(
                    200, content=(FIXTURES / "nomis-definition.json").read_bytes()
                )
            if path == "/api/v01/dataset/NM_162_1.data.csv":
                assert request.url.params["date"] == "2025-01-2025-03"
                assert request.url.params["measures"] == "001"
                offset = int(request.url.params["recordoffset"])
                size = int(request.url.params["recordlimit"])
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=list(self.nomis_rows[0]))
                writer.writeheader()
                writer.writerows(self.nomis_rows[offset : offset + size])
                return httpx.Response(
                    200, text=output.getvalue(), headers={"content-type": "text/csv"}
                )
        if self.provider == "statswales" and request.url.host == "api.stats.gov.wales":
            base = f"/v2/{statswales.DEFAULT_ID}"
            if request.method == "POST" and path == base + "/data":
                assert json.loads(request.content) == {
                    "filters": [{"AreaID": ["001", "002", "003"]}, {"YearID": ["2025"]}],
                    "options": {"data_value_type": "formatted"},
                }
                return httpx.Response(200, json={"filterId": "fixture00001"})
            if path == base + "/filters":
                return httpx.Response(200, json=self.sw["dimensions"])
            if path == base + "/query/fixture00001":
                return httpx.Response(
                    200,
                    json={
                        "id": "fixture00001",
                        "datasetId": statswales.DEFAULT_ID,
                        "revisionId": "fixture-revision-1",
                        "totalLines": len(self.sw["rows"]),
                    },
                )
            if path == base:
                return httpx.Response(200, json=self.sw["metadata"])
            if path == base + "/data/fixture00001":
                assert request.url.params["format"] == "json"
                page = int(request.url.params["page_number"])
                size = int(request.url.params["page_size"])
                return httpx.Response(200, json=self.sw["rows"][(page - 1) * size : page * size])
        if self.provider == "deprivation" and str(request.url) == deprivation.SOURCE_URL:
            return httpx.Response(
                200,
                content=(FIXTURES / "deprivation.csv").read_bytes(),
                headers={"content-type": "text/csv"},
            )
        raise AssertionError(f"Unexpected fixture request: {request.method} {request.url}")


@contextmanager
def fixture_client(provider: str):
    """Checked synthetic transport with private pacing; live settings stay intact."""
    responses = FixtureResponses(provider)
    with TemporaryDirectory(prefix="ssdu-fixture-rate-") as temporary:
        limiter = RateLimiter(Path(temporary) / "rate.sqlite3", interval_seconds=0)
        with HttpClient(
            transport=httpx.MockTransport(responses), retries=0, rate_limiter=limiter
        ) as client:
            client.fixture_responses = responses
            yield client


@contextmanager
def no_network():
    """Fail and record any socket connection, including accidental live HTTP."""
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("network connection")
        raise AssertionError("Network disabled in the executable contract example")

    with (
        patch.object(socket.socket, "connect", forbidden),
        patch.object(socket.socket, "connect_ex", forbidden),
        patch.object(socket, "create_connection", forbidden),
    ):
        yield attempts


def check_dataset(dataset, provider: str) -> dict:
    """Check meanings, raw tokens, code strings and the common observation schema."""
    table = dataset.to_arrow("observations")
    assert table.schema.field("measure").type == pa.string()
    assert table.schema.field("value").type == pa.float64()
    for name in ("value_raw", "unit", "status", "notes"):
        assert table.schema.field(name).type == pa.string()
    frame = dataset.to_pandas("observations")
    assert len(frame) == table.num_rows
    if provider == "nomis":
        assert table["value"].to_pylist() == [1.0, None, 0.0]
        assert table["value_raw"].to_pylist() == ["001", "..", "0"]
        assert table["status"].to_pylist() == ["A", "S", "A"]
        assert table["geography"].to_pylist() == ["K02000001"] * 3
    elif provider == "statswales":
        assert table["value"].to_pylist() == [22.0, None, 1.0]
        assert table["value_raw"].to_pylist() == ["          22", "*", "001"]
        assert table["area"].to_pylist() == ["001", "002", "003"]
        assert table["notes"].to_pylist() == [None, "suppressed", "p"]
    else:
        rows = table.to_pylist()
        scores = [row for row in rows if row["measure"] == "imd_score"]
        assert [row["value"] for row in scores] == [4.125, None, 6.25]
        assert [row["value_raw"] for row in scores] == ["4.125", "[c]", "6.25"]
        assert [row["lsoa_code"] for row in scores] == ["E01000001", "E01027305", "E01000002"]
        assert table.num_rows == 15
        rank = [row for row in rows if row["measure"] == "imd_rank"]
        assert [row["value"] for row in rank] == [12001.0, 22002.0, 33003.0]
    assert dataset.to_arrow("source").num_rows == 3
    descriptor = dataset.contract
    assert descriptor["quality"]["source_rows"] == 3
    assert descriptor["quality"]["observation_rows"] == table.num_rows
    return {
        "source_rows": 3,
        "observation_rows": table.num_rows,
        "schema": str(table.schema),
        "snapshot_id": dataset.snapshot_id,
    }


def run(output_dir: str | Path) -> dict:
    """Acquire, normalize, cache, pin, transfer and reopen all three providers."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    return _run(output)


def _run(output: Path) -> dict:
    manifest = fixture_manifest()
    source = Store(output / "source")
    datasets, receipts = {}, {}
    with no_network() as attempts:
        for provider in PROVIDERS:
            with fixture_client(provider) as client:
                datasets[provider] = source.fetch_dataset(request_for(provider), client=client)
                receipts[provider] = check_dataset(datasets[provider], provider)
                receipts[provider]["fixture_http_attempts"] = client.attempts
                assert client.attempts == len(client.fixture_responses.calls) > 0

            # A second acquisition and explicit offline acquisition must read retained data.
            def forbidden_http(*args, **kwargs):
                raise AssertionError("Cache reuse contacted a provider")

            with patch.object(HttpClient, "request", forbidden_http):
                cached = source.fetch_dataset(request_for(provider))
                offline = source.fetch_dataset(request_for(provider), offline=True)
                opened = source.open_dataset(datasets[provider].snapshot_id)
            assert (
                cached.snapshot_id
                == offline.snapshot_id
                == opened.snapshot_id
                == datasets[provider].snapshot_id
            )
            assert opened.contract == datasets[provider].contract
            assert opened.to_arrow().equals(datasets[provider].to_arrow())
            receipts[provider]["cached_requests"] = 0
        lock = Collections(source).resolve(
            {
                "name": "contract-examples",
                "description": "Synthetic three-provider dataset-contract teaching fixtures",
                "entries": {p: {"snapshot_id": d.snapshot_id} for p, d in datasets.items()},
            },
            offline=True,
        )
        bundle = output / "contract-examples.ssdu.zip"
        exported = export_bundle(source, lock, bundle)
        destination = Store(output / "fresh")
        imported = import_bundle(destination, bundle)
        with patch.object(HttpClient, "request", forbidden_http):
            for provider, dataset in datasets.items():
                replayed = destination.open_dataset(dataset.snapshot_id)
                assert replayed.contract == dataset.contract
                assert replayed.to_arrow().equals(dataset.to_arrow())
                check_dataset(replayed, provider)
            assert Collections(destination).replay(lock.lock_id) == Collections(source).replay(
                lock.lock_id
            )
        assert exported["bundle_id"] == imported["bundle_id"]
        assert attempts == []
    result = {
        "verified": True,
        "fixture_kind": manifest["kind"],
        "fixture_files": manifest["files"],
        "providers": receipts,
        "collection_lock_id": lock.lock_id,
        "bundle_id": exported["bundle_id"],
        "fresh_store_equal": True,
        "provider_network_requests": 0,
        "network_attempts": len(attempts),
    }
    (output / "result.json").write_bytes(canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data/validation/dataset-contract"))
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
