"""CLI contract reads match Python results from retained synthetic snapshots."""

import json
import socket
from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_dataset_store import historical_snapshot, provider_client

from ssdu import Store
from ssdu.adapters.nomis import default_request
from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.local_import import import_local
from ssdu.service import Service


def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Contract CLI attempted network access")

    monkeypatch.setattr(HttpClient, "__init__", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture(params=["provider", "records"])
def retained(request, tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    if request.param == "provider":
        with provider_client() as client:
            dataset = store.fetch_dataset(default_request(), client=client)
    else:
        source = tmp_path / "invented.csv"
        source.write_text("code,label,count\n001,Invented A,3\n002,Invented B,\n")
        manifest = import_local(
            store,
            source,
            {
                "dataset_id": "invented-cli",
                "title": "Invented CLI fixture",
                "column_types": {"count": "int64"},
                "primary_key": ["code"],
            },
        )
        dataset = store.open_dataset(manifest["product_id"])
    forbid_network(monkeypatch)
    return store, dataset, Service(store.root, index_store=False)


def invoke(store, capsys, *args):
    assert main(["--store", str(store.root), "dataset", *args]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def test_cli_describe_and_query_equal_python(retained, tmp_path, capsys):
    store, dataset, service = retained
    assert invoke(store, capsys, "describe", dataset.snapshot_id) == service.describe_snapshot(
        dataset.snapshot_id
    )
    query = {"limit": 1, "offset": 0}
    path = tmp_path / "query.json"
    path.write_text(json.dumps(query))
    result = invoke(store, capsys, "query", dataset.snapshot_id, "--query", str(path))
    assert result == service.query_dataset(dataset.snapshot_id, query)
    assert len(result["rows"]) == 1
    assert result["next_offset"] == 1
    if dataset.descriptor.profile == "records":
        assert result["rows"][0]["code"] == "001"


def test_cli_explicit_source_query_equal_python(retained, tmp_path, capsys):
    store, dataset, service = retained
    query = {"table": "source", "limit": 2}
    path = tmp_path / "query.json"
    path.write_text(json.dumps(query))
    result = invoke(store, capsys, "query", dataset.snapshot_id, "--query", str(path))
    assert result == service.query_dataset(dataset.snapshot_id, query)
    assert result["table"] == "source"


def test_cli_keeps_identifiers_missingness_and_suppression(retained, tmp_path, capsys):
    store, dataset, _ = retained
    query_path = tmp_path / "query.json"
    query_path.write_text('{"limit": 10}')
    result = invoke(store, capsys, "query", dataset.snapshot_id, "--query", str(query_path))
    if dataset.descriptor.profile == "records":
        assert {row["code"]: row["count"] for row in result["rows"]} == {
            "001": 3,
            "002": None,
        }
    else:
        assert {row["value_raw"]: row["value"] for row in result["rows"]} == {
            "001": 1.0,
            "..": None,
            "0": 0.0,
        }
        suppressed = next(row for row in result["rows"] if row["value_raw"] == "..")
        assert suppressed["status"]


def test_cli_dimensions_equal_python(retained, capsys):
    store, dataset, service = retained
    dimension = (
        "code" if dataset.descriptor.profile == "records" else dataset.descriptor.dimensions[0].id
    )
    result = invoke(
        store,
        capsys,
        "dimensions",
        dataset.snapshot_id,
        dimension,
        "--search",
        "0",
        "--limit",
        "1",
        "--offset",
        "0",
    )
    assert result == service.list_snapshot_dimension_values(
        dataset.snapshot_id, dimension, search="0", limit=1, offset=0
    )


@pytest.mark.parametrize("snapshot_id", ["bad", "a" * 64])
@pytest.mark.parametrize("action", ["describe", "query", "dimensions"])
def test_invalid_snapshot_is_a_cli_error(tmp_path, monkeypatch, capsys, snapshot_id, action):
    forbid_network(monkeypatch)
    arguments = ["--store", str(tmp_path / "store"), "dataset", action, snapshot_id]
    if action == "query":
        path = tmp_path / "query.json"
        path.write_text("{}")
        arguments.extend(["--query", str(path)])
    elif action == "dimensions":
        arguments.append("area")
    assert main(arguments) == 1
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("ssdu: ")
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    "query",
    [
        {"table": "arbitrary"},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 100_001},
        {"columns": ["unknown-column"]},
        {"sql": "SELECT * FROM data"},
    ],
)
def test_invalid_query_is_a_cli_error(retained, tmp_path, capsys, query):
    store, dataset, _ = retained
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(query))
    assert (
        main(
            [
                "--store",
                str(store.root),
                "dataset",
                "query",
                dataset.snapshot_id,
                "--query",
                str(path),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("ssdu: ")
    assert "Traceback" not in output.err


def test_unavailable_profile_table_is_a_cli_error(retained, tmp_path, capsys):
    _, dataset, _ = retained
    table = "observations" if dataset.descriptor.profile == "records" else "records"
    test_invalid_query_is_a_cli_error(retained, tmp_path, capsys, {"table": table})


@pytest.mark.parametrize(
    "options",
    [
        ["--limit", "0"],
        ["--limit", "101"],
        ["--offset", "-1"],
        ["--offset", "100001"],
        ["--search", "a" * 513],
    ],
)
def test_invalid_dimension_bounds_are_cli_errors(retained, capsys, options):
    store, dataset, _ = retained
    assert (
        main(
            [
                "--store",
                str(store.root),
                "dataset",
                "dimensions",
                dataset.snapshot_id,
                "unknown",
                *options,
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("ssdu: ")
    assert "Traceback" not in output.err


@pytest.mark.parametrize("body", [b"{broken", b" " * 256_001])
def test_malformed_or_oversized_query_file_fails_bounded(tmp_path, monkeypatch, capsys, body):
    forbid_network(monkeypatch)
    path = tmp_path / "query.json"
    path.write_bytes(body)
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "dataset",
                "query",
                "a" * 64,
                "--query",
                str(path),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert len(output.err) < 2000
    assert "Traceback" not in output.err


def test_describe_does_not_migrate_historical_snapshots(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "store")
    snapshot_id = historical_snapshot(store)["product_id"]
    forbid_network(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-triggered migration")

    monkeypatch.setattr(Store, "migrate_dataset", forbidden)
    assert main(["--store", str(store.root), "dataset", "describe", snapshot_id]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "migrate" in output.err
    assert len(store.list()) == 1


@pytest.mark.parametrize("timestamp_unit", ["us", "ns"])
def test_records_show_preview_preserves_temporal_decimal_values(
    tmp_path, monkeypatch, capsys, timestamp_unit
):
    store = Store(tmp_path / "store")
    source = tmp_path / "invented.parquet"
    timestamp = (
        pd.Timestamp("2026-01-02T03:04:00.123456789Z")
        if timestamp_unit == "ns"
        else datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    )
    table = pa.table(
        {
            "code": ["001"],
            "date": pa.array([date(2026, 1, 2)], type=pa.date32()),
            "timestamp": pa.array(
                [timestamp],
                type=pa.timestamp(timestamp_unit, tz="UTC"),
            ),
            "time": pa.array([3_723_000_000_123], type=pa.time64("ns")),
            "amount": pa.array([Decimal("1.23")], type=pa.decimal128(8, 2)),
        }
    )
    pq.write_table(table, source)
    manifest = import_local(
        store,
        source,
        {"dataset_id": "invented-types", "title": "Invented typed CLI fixture"},
    )
    forbid_network(monkeypatch)
    result = invoke(store, capsys, "show", manifest["product_id"], "--preview", "1")
    assert result["preview"] == [
        {
            "code": "001",
            "date": "2026-01-02",
            "timestamp": "2026-01-02T03:04:00.123456789+00:00"
            if timestamp_unit == "ns"
            else "2026-01-02T03:04:00+00:00",
            "amount": "1.23",
            "time": "01:02:03.000000123",
        }
    ]


def test_show_preview_preserves_retained_row_order(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "store")
    source = tmp_path / "invented-order.csv"
    source.write_text("code\n002\n001\n")
    manifest = import_local(
        store,
        source,
        {"dataset_id": "invented-order", "title": "Invented row order fixture"},
    )
    forbid_network(monkeypatch)
    result = invoke(store, capsys, "show", manifest["product_id"], "--preview", "1")
    assert result["preview"] == [{"code": "002"}]


@pytest.mark.parametrize("command", ["describe", "query", "dimensions"])
def test_contract_cli_help_is_available(command, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["dataset", command, "--help"])
    assert exc.value.code == 0
    assert "snapshot_id" in capsys.readouterr().out
