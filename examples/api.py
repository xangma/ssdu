"""Real REST + MCP clients against frozen data, with provider networking blocked.

Run: uv run --offline python examples/api.py --offline
Default: England-wide decile 1, first 100 rows ordered by deprivation rank.
Use --local-authority-code CODE and --decile 1..10 for a narrower query.
Use --derive-authority to verify an additional local selection without a download.
Preparation may use HTTP on a fresh store unless --offline is supplied.
Both servers are temporary and are always stopped after verification.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import Client, StdioServerParameters

from ssdu.adapters import deprivation
from ssdu.http import HttpClient
from ssdu.models import Request
from ssdu.shared import SharedDeprivation
from ssdu.store import Store

# This guard runs inside both server subprocesses before application imports.
# Localhost is allowed for REST; providers, DNS and all other networks are denied.
GUARD = """
import socket
from ssdu.http import HttpClient
def denied(*args, **kwargs):
    raise AssertionError("Offline API attempted provider HTTP")
HttpClient.request = denied
def local(host):
    return host in ("127.0.0.1", "::1", "localhost")
original_dns = socket.getaddrinfo
def dns(host, *args, **kwargs):
    if not local(host):
        raise AssertionError("Offline API attempted external DNS")
    return original_dns(host, *args, **kwargs)
socket.getaddrinfo = dns
for method in ("connect", "connect_ex"):
    original = getattr(socket.socket, method)
    def guarded(self, address, _original=original):
        if not isinstance(address, tuple) or not local(address[0]):
            raise AssertionError("Offline API attempted external socket connection")
        return _original(self, address)
    setattr(socket.socket, method, guarded)
"""


def assert_ok(response):
    response.raise_for_status()
    return response.json()


async def mcp_check(
    root, env, snapshot_id, query, rest_result, metadata, dimension, dimension_search, dimensions
):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ssdu.cli", "--store", str(root), "mcp"],
        env=env,
    )
    async with Client(parameters, read_timeout_seconds=15) as client:
        listing = await client.list_tools()
        assert {
            "search_datasets",
            "describe_dataset",
            "list_dimension_values",
            "preview_data",
            "query_data",
            "get_provenance",
        } <= {tool.name for tool in listing.tools}
        found = await client.call_tool("search_datasets", {"query": "deprivation"})
        assert not found.is_error
        key = metadata["dataset_id"]
        described = await client.call_tool("describe_dataset", {"dataset_id": key})
        assert not described.is_error and described.structured_content == metadata
        values = await client.call_tool(
            "list_dimension_values",
            {"dataset_id": key, "dimension": dimension, "search": dimension_search},
        )
        assert not values.is_error and values.structured_content == dimensions
        for tool in ("preview_data", "query_data"):
            result = await client.call_tool(tool, {"snapshot_id": snapshot_id, "query": query})
            assert not result.is_error
            assert result.structured_content == rest_result
        count = await client.call_tool(
            "query_data", {"snapshot_id": snapshot_id, "query": {**query, "count_only": True}}
        )
        assert not count.is_error
        assert count.structured_content["rows"] == [{"count": rest_result["total_rows"]}]
        provenance = await client.call_tool("get_provenance", {"snapshot_id": snapshot_id})
        assert (
            not provenance.is_error and provenance.structured_content == rest_result["provenance"]
        )
        resource = await client.read_resource(f"ssdu://snapshots/{snapshot_id}/provenance")
        assert json.loads(resource.contents[0].text) == rest_result["provenance"]


def run(root="data", offline=False, *, local_authority_code=None, decile=1, derive_authority=False):
    """Verify REST/MCP parity against a national snapshot, with optional area selection.

    Preparation can acquire the shared source unless ``offline=True``. Both API
    servers always block provider access, stop after verification, and leave a
    result and REST log under ``<root>/validation``. Empty selections are valid.
    """
    if type(decile) is not int or not 1 <= decile <= 10:
        raise ValueError("Decile must be an integer from 1 to 10")
    store = Store(root)
    guard = (
        patch.object(
            HttpClient,
            "request",
            side_effect=AssertionError("Offline preparation attempted provider HTTP"),
        )
        if offline
        else nullcontext()
    )
    with guard:
        source = SharedDeprivation(store).source(offline=offline)
    snapshot_id = source["product_id"]
    frame = store.replay(snapshot_id)
    authority_codes = sorted(frame["local_authority_code"].dropna().unique().tolist())
    selected = frame
    if local_authority_code is not None:
        if local_authority_code not in authority_codes:
            raise ValueError("Local authority code is absent from the retained source")
        selected = selected.loc[selected["local_authority_code"].eq(local_authority_code)]
    matched = selected.loc[selected["imd_decile"].eq(decile)].sort_values(["imd_rank", "lsoa_code"])
    expected = matched["lsoa_code"].head(100).tolist()
    query = {
        "columns": ["lsoa_code", "lsoa_name", "imd_score", "imd_rank", "imd_decile"],
        "filters": [
            {"column": "imd_decile", "value": decile},
        ],
        "sort": [{"column": "imd_rank"}],
        "limit": 100,
    }
    if local_authority_code is not None:
        query["filters"].append({"column": "local_authority_code", "value": local_authority_code})
    validation = store.root / "validation"
    validation.mkdir(exist_ok=True)
    log_path = validation / "api-rest.log"
    with tempfile.TemporaryDirectory(prefix="ssdu-offline-") as guard_dir:
        Path(guard_dir, "sitecustomize.py").write_text(GUARD)
        env = {
            **os.environ,
            "PYTHONPATH": guard_dir + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        command = [
            sys.executable,
            "-m",
            "ssdu.cli",
            "--store",
            str(store.root),
            "serve",
            "--port",
            str(port),
        ]
        with log_path.open("w") as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            print(
                json.dumps(
                    {
                        "host": "local",
                        "cwd": str(Path.cwd()),
                        "command": command,
                        "pid": server.pid,
                        "log": str(log_path),
                        "next_check": "REST/MCP verification within 15 seconds",
                        "stop": f"kill {server.pid}; normally stopped automatically",
                    }
                ),
                flush=True,
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as api:
                    deadline = time.monotonic() + 15
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError(f"REST server exited; see {log_path}")
                        try:
                            assert_ok(api.get("/health"))
                            break
                        except httpx.ConnectError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError(
                                    f"REST startup timed out; see {log_path}"
                                ) from None
                            time.sleep(0.1)
                    assert_ok(api.get("/datasets", params={"query": "deprivation"}))
                    key = f"{source['provider']}:{source['dataset_id']}"
                    metadata = assert_ok(api.get(f"/datasets/{key}"))
                    dimension = next(
                        column["name"]
                        for column in metadata["columns"]
                        if column.get("role") == "identifier"
                    )
                    first_values = assert_ok(
                        api.get(f"/datasets/{key}/dimensions/{dimension}", params={"limit": 1})
                    )
                    assert first_values["items"], "No retained identifier values are available"
                    dimension_search = str(first_values["items"][0]["value"])
                    dimensions = assert_ok(
                        api.get(
                            f"/datasets/{key}/dimensions/{dimension}",
                            params={"search": dimension_search},
                        )
                    )
                    result = assert_ok(api.post(f"/snapshots/{snapshot_id}/query", json=query))
                    assert result["total_rows"] == len(matched)
                    assert [row["lsoa_code"] for row in result["rows"]] == expected
                    assert result["truncated"] is (len(matched) > 100)
                    assert result["provenance"]["snapshot_id"] == snapshot_id
                    assert result["provenance"]["metadata_version"] == metadata["metadata_version"]
                    assert_ok(api.get("/openapi.json"))
                    asyncio.run(
                        mcp_check(
                            store.root,
                            env,
                            snapshot_id,
                            query,
                            result,
                            metadata,
                            dimension,
                            dimension_search,
                            dimensions,
                        )
                    )
                    derived = None
                    if derive_authority:
                        other_code = next(
                            (code for code in authority_codes if code != local_authority_code),
                            None,
                        )
                        if other_code is not None:
                            with patch.object(
                                HttpClient,
                                "request",
                                side_effect=AssertionError(
                                    "Local selection attempted provider HTTP"
                                ),
                            ):
                                other = SharedDeprivation(store).fetch(
                                    Request(
                                        "deprivation",
                                        deprivation.DATASET_ID,
                                        {"local_authority_codes": [other_code]},
                                    ),
                                    offline=True,
                                    source_snapshot_id=snapshot_id,
                                )
                            assert other["parent_snapshot_ids"] == [snapshot_id]
                            assert not other["resources"]
                            derived = {
                                "local_authority_code": other_code,
                                "rows": other["rows"],
                                "provider_requests": 0,
                            }
                    result["verification"] = {
                        "source_rows": len(frame),
                        "selected_rows": len(selected),
                        "scope": "local_authority" if local_authority_code else "england",
                        "local_authority_code": local_authority_code,
                        "decile": decile,
                        "matching_rows": len(matched),
                        "returned_rows": len(expected),
                        "dimension_checked": dimension,
                        "rest_mcp_equal": True,
                        "external_network_blocked": True,
                        "derived_selection": derived,
                    }
                    output = validation / "api-result.json"
                    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
                    print(
                        json.dumps(
                            {
                                **result["verification"],
                                "snapshot_id": snapshot_id,
                                "result": str(output),
                            },
                            indent=2,
                        )
                    )
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--store", default="data")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--local-authority-code")
    parser.add_argument("--decile", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--derive-authority", action="store_true")
    args = parser.parse_args(argv)
    return run(
        args.store,
        args.offline,
        local_authority_code=args.local_authority_code,
        decile=args.decile,
        derive_authority=args.derive_authority,
    )


if __name__ == "__main__":
    main()
