"""Verified snapshot queries use contract semantics and retain scalar precision."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError
from test_dataset_store import historical_snapshot, provider_client

from ssdu import Dataset, Store
from ssdu.adapters.nomis import default_request, fetch
from ssdu.contracts import DatasetQuerySpec, QuerySpec
from ssdu.http import HttpClient
from ssdu.local_import import import_local
from ssdu.models import RetrievalError
from ssdu.service import Service
from ssdu.store import canonical


@pytest.fixture
def observations(tmp_path, monkeypatch):
    store = Store(tmp_path / "provider-store")
    request = default_request()
    with provider_client() as client:
        acquisition = fetch(request, client)
        descriptor = acquisition.dataset.contract
        geography = next(item for item in descriptor["dimensions"] if item["id"] == "geography")
        geography["codes"] = [
            {"code": "K02000001", "label": "Invented retained area"},
            {"code": "UNOBSERVED", "label": "Invented unobserved area"},
        ]
        acquisition.dataset = Dataset.from_tables(
            descriptor,
            {name: acquisition.dataset.to_arrow(name) for name in acquisition.dataset.table_names},
        )
        manifest = store._publish(
            request,
            hashlib.sha256(canonical(request.to_dict())).hexdigest(),
            acquisition,
            client.resources,
        )
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    monkeypatch.setattr(Store, "migrate_dataset", lambda *a, **k: pytest.fail("Read migrated data"))
    return Service(store.root, index_store=False), manifest["product_id"]


@pytest.fixture
def records(tmp_path, monkeypatch):
    table = pa.table(
        {
            "code": ["003", "001", "002"],
            "amount": pa.array(
                [
                    Decimal("123456789012345678901234567890.123456789012345678901234567891"),
                    Decimal("123456789012345678901234567890.123456789012345678901234567890"),
                    None,
                ],
                type=pa.decimal256(60, 30),
            ),
            "stamp": pa.array(
                [1234567890123456789, 1234567890123456788, None],
                type=pa.timestamp("ns", "UTC"),
            ),
            "clock": pa.array([1234567890123, 1234567890122, None], type=pa.time64("ns")),
            "as_of": pa.array([date(2026, 1, 1), date(2026, 1, 2), None], type=pa.date32()),
            "unsigned": pa.array([2**64 - 1, 2**63 + 3, 0], type=pa.uint64()),
            "approved": [True, False, None],
            "token": ["[s]", "", None],
        }
    )
    source = tmp_path / "invented.parquet"
    pq.write_table(table, source)
    store = Store(tmp_path / "records-store")
    manifest = import_local(
        store,
        source,
        {
            "dataset_id": "invented",
            "title": "Invented records for conformance",
            "primary_key": ["code"],
            "row_grain": "One invented record per code",
            "columns": {
                "code": {"label": "Invented code"},
                "amount": {"unit": "GBP", "definition": "An invented exact decimal"},
                "token": {"missing_values": {"[s]": "Suppressed", "": "Not supplied"}},
            },
        },
    )
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    monkeypatch.setattr(Store, "migrate_dataset", lambda *a, **k: pytest.fail("Read migrated data"))
    return Service(store.root, index_store=False), manifest["product_id"]


def test_observations_default_normalized_source_explicit_and_legacy_unchanged(observations):
    service, snapshot_id = observations
    original_spec = QuerySpec(columns=["OBS_VALUE"], limit=2)
    original_bytes = canonical(original_spec.model_dump())
    before = service.query_data(snapshot_id, original_spec)
    normalized = service.query_dataset(snapshot_id, {})
    source = service.query_dataset(snapshot_id, {"table": "source", "columns": ["OBS_VALUE"]})
    assert normalized["table"] == "observations"
    assert normalized["profile"] == "observations"
    assert normalized["contract_version"] == "1.0.0"
    assert [row["value"] for row in normalized["rows"]] == [0.0, 1.0, None]
    assert {row["OBS_VALUE"] for row in source["rows"]} == {"001", "..", "0"}
    assert source["effective_query"]["table"] == "source"
    assert "table" not in before["effective_query"]
    assert service.query_data(snapshot_id, original_spec) == before
    assert canonical(original_spec.model_dump()) == original_bytes


def test_null_suppression_status_and_unit_context_remain_visible(observations):
    service, snapshot_id = observations
    result = service.query_dataset(
        snapshot_id,
        {"filters": [{"column": "value", "value": None}, {"column": "status", "value": "S"}]},
    )
    assert result["rows"][0]["value"] is None
    assert result["rows"][0]["value_raw"] == ".."
    assert result["rows"][0]["status"] == "S"
    assert result["units"] == {
        "value_column": "unit",
        "variables": {"OBS_VALUE": None},
        "columns": {},
    }
    assert result["column_types"]["value"] == "float64"
    assert any(item["code"] == "unknown_unit" for item in result["quality"]["diagnostics"])
    count = service.query_dataset(
        snapshot_id,
        {"count_only": True, "filters": [{"column": "value", "op": "in", "value": [0, None]}]},
    )
    assert count["rows"] == [{"count": 2}]
    assert count["column_types"] == {"count": "int64"}


def test_describe_is_explicit_summary_and_dimensions_are_observed_and_pinned(observations):
    service, snapshot_id = observations
    description = service.describe_snapshot(snapshot_id)
    assert description["snapshot_id"] == snapshot_id
    assert description["default_table"] == "observations"
    assert description["verification"] == {
        "file_hashes": "verified",
        "contract": "validated",
        "offline": True,
    }
    geography = next(
        item for item in description["descriptor"]["dimensions"] if item["id"] == "geography"
    )
    assert geography["codes_omitted"] is True and geography["code_count"] == 2
    assert "codes" not in geography
    result = service.list_snapshot_dimension_values(snapshot_id, "geography", search="RETAINED")
    assert result["items"] == [{"value": "K02000001", "label": "Invented retained area"}]
    assert result["total"] == 1 and result["snapshot_id"] == snapshot_id
    assert (
        service.list_snapshot_dimension_values(snapshot_id, "geography", "UNOBSERVED")["items"]
        == []
    )


def test_frozen_pagination_has_full_row_tie_breakers(observations):
    service, snapshot_id = observations
    spec = {"columns": ["value_raw"], "sort": [{"column": "status"}], "limit": 1}
    pages = [service.query_dataset(snapshot_id, {**spec, "offset": offset}) for offset in range(4)]
    assert [item["rows"] for item in pages] == [
        [{"value_raw": "0"}],
        [{"value_raw": "001"}],
        [{"value_raw": ".."}],
        [],
    ]
    assert [item["next_offset"] for item in pages] == [1, 2, None, None]
    assert service.query_dataset(snapshot_id, spec) == pages[0]


def test_records_description_preserves_declared_semantics_and_identifiers(records):
    service, snapshot_id = records
    description = service.describe_snapshot(snapshot_id)
    descriptor = description["descriptor"]
    assert description["contract_version"] == "1.1.0" and description["default_table"] == "records"
    assert descriptor["primary_key"] == ["code"]
    assert descriptor["row_grain"] == "One invented record per code"
    assert descriptor["dimensions"] == [] and descriptor["variables"] == []
    fields = {item["name"]: item for item in descriptor["tables"][0]["columns"]}
    assert fields["token"]["missing_values"] == {"[s]": "Suppressed", "": "Not supplied"}
    assert fields["amount"]["unit"] == "GBP"
    first = service.list_snapshot_dimension_values(snapshot_id, "code", limit=1)
    second = service.list_snapshot_dimension_values(snapshot_id, "code", limit=1, offset=1)
    assert first["items"] == [{"value": "001", "label": "001"}]
    assert first["next_offset"] == 1 and second["items"] == [{"value": "002", "label": "002"}]
    with pytest.raises(ValueError, match="declared"):
        service.list_snapshot_dimension_values(snapshot_id, "amount")


def test_records_exact_precision_json_types_and_source_parity(records):
    service, snapshot_id = records
    result = service.query_dataset(snapshot_id, {"filters": [{"column": "code", "value": "003"}]})
    row = result["rows"][0]
    assert row["code"] == "003"
    assert row["amount"] == "123456789012345678901234567890.123456789012345678901234567891"
    assert row["stamp"] == "2009-02-13T23:31:30.123456789+00:00"
    assert row["clock"] == "00:20:34.567890123"
    assert row["as_of"] == "2026-01-01" and row["unsigned"] == 2**64 - 1
    assert row["approved"] is True and row["token"] == "[s]"
    assert result["units"]["columns"] == {"amount": "GBP"}
    assert result["column_types"]["amount"] == "decimal256(60, 30)"
    assert result["column_types"]["unsigned"] == "uint64"
    assert "Exact" in result["value_encoding"]["decimal"]
    assert service.query_dataset(
        snapshot_id, {"table": "source", "filters": [{"column": "code", "value": "003"}]}
    )["rows"] == [row]
    assert json.loads(json.dumps(result, allow_nan=False))["rows"] == [row]


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("amount", "123456789012345678901234567890.123456789012345678901234567891", ["003"]),
        ("stamp", "2009-02-13T23:31:30.123456789+00:00", ["003"]),
        ("clock", "00:20:34.567890123", ["003"]),
        ("unsigned", 2**64 - 1, ["003"]),
        ("unsigned", -1, []),
        ("approved", False, ["001"]),
        ("token", "", ["001"]),
        ("token", None, ["002"]),
    ],
)
def test_record_filters_do_not_round_temporal_decimal_or_unsigned_values(
    records, column, value, expected
):
    service, snapshot_id = records
    result = service.query_dataset(
        snapshot_id, {"columns": ["code"], "filters": [{"column": column, "value": value}]}
    )
    assert [row["code"] for row in result["rows"]] == expected


@pytest.mark.parametrize("column", ["amount", "stamp", "clock", "unsigned"])
def test_record_native_sort_keeps_fine_precision(records, column):
    service, snapshot_id = records
    result = service.query_dataset(snapshot_id, {"columns": ["code"], "sort": [{"column": column}]})
    expected = ["002", "001", "003"] if column == "unsigned" else ["001", "003", "002"]
    assert [row["code"] for row in result["rows"]] == expected


@pytest.mark.parametrize(
    "spec",
    [
        {"table": "../../raw"},
        {"table": "records"},
        {"columns": ["value", "value"]},
        {"columns": ["value; DROP TABLE data"]},
        {"filters": [{"column": "missing", "value": "anything"}]},
        {"filters": [{"column": "value", "value": "1"}]},
        {"filters": [{"column": "geography", "value": 1}]},
        {"filters": [{"column": "value", "op": "gte", "value": None}]},
        {"sort": [{"column": "unknown"}]},
        {"limit": True},
        {"limit": 101},
        {"offset": -1},
        {"sql": "SELECT 1"},
    ],
)
def test_contract_query_rejects_unregistered_tables_columns_operands_and_bounds(observations, spec):
    service, snapshot_id = observations
    with pytest.raises(ValueError):
        service.query_dataset(snapshot_id, spec)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "1.2 OR TRUE"])
def test_decimal_filters_fail_closed(records, value):
    service, snapshot_id = records
    with pytest.raises(ValueError, match="Decimal filters"):
        service.query_dataset(snapshot_id, {"filters": [{"column": "amount", "value": value}]})


@pytest.mark.parametrize(
    "options", [{"limit": True}, {"limit": 0}, {"offset": -1}, {"search": "x" * 513}]
)
def test_snapshot_dimension_bounds_are_strict(observations, options):
    service, snapshot_id = observations
    with pytest.raises(ValueError, match="bounds"):
        service.list_snapshot_dimension_values(snapshot_id, "geography", **options)


def test_contract_reads_preserve_retained_bytes_and_cannot_silently_repair(observations):
    service, snapshot_id = observations
    root = service.store.path(snapshot_id)
    before = {path.name: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    service.describe_snapshot(snapshot_id)
    service.query_dataset(snapshot_id, {})
    service.list_snapshot_dimension_values(snapshot_id, "geography")
    assert {path.name: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    (root / "tables/observations.parquet").write_bytes(b"corrupt")
    for read in (
        service.describe_snapshot,
        lambda key: service.query_dataset(key, {}),
        lambda key: service.list_snapshot_dimension_values(key, "geography"),
    ):
        with pytest.raises(RetrievalError, match="hash mismatch"):
            read(snapshot_id)


def test_historical_read_requires_explicit_migration_and_keeps_legacy_query(tmp_path, monkeypatch):
    store = Store(tmp_path)
    snapshot_id = historical_snapshot(store)["product_id"]
    service = Service(tmp_path, index_store=False)
    monkeypatch.setattr(Store, "migrate_dataset", lambda *a, **k: pytest.fail("Implicit migration"))
    assert service.query_data(snapshot_id, QuerySpec(count_only=True))["rows"] == [{"count": 3}]
    with pytest.raises(RetrievalError, match="migrate_dataset"):
        service.describe_snapshot(snapshot_id)
    with pytest.raises(RetrievalError, match="migrate_dataset"):
        service.query_dataset(snapshot_id, {})


def test_input_and_cooperative_execution_budgets_are_enforced(observations, monkeypatch):
    import ssdu.service as module

    service, snapshot_id = observations
    monkeypatch.setattr(module, "MAX_DATASET_QUERY_ROWS", 1)
    monkeypatch.setattr(
        Store, "open_dataset", lambda *a, **k: pytest.fail("Oversized input decoded")
    )
    with pytest.raises(ValueError, match="row budget"):
        service.query_dataset(snapshot_id, {})
    monkeypatch.undo()
    monkeypatch.setattr(module, "MAX_DATASET_QUERY_BYTES", 1)
    with pytest.raises(ValueError, match="byte budget"):
        service.query_dataset(snapshot_id, {})
    monkeypatch.undo()
    times = iter([0, 6])
    monkeypatch.setattr(module, "monotonic", lambda: next(times))
    with pytest.raises(ValueError, match="cooperative"):
        service.query_dataset(snapshot_id, {"count_only": True})


def test_response_bounds_reject_large_semantics_instead_of_silent_truncation(records, monkeypatch):
    service, snapshot_id = records
    dataset = service.store.open_dataset(snapshot_id)
    descriptor = dataset.contract
    descriptor["title"] = "x" * 100_000
    descriptor["source"]["citation"] = "x" * 100_000
    descriptor["source"]["url"] = "x" * 100_000
    changed = Dataset.from_tables(
        descriptor,
        {name: dataset.to_arrow(name) for name in dataset.table_names},
        snapshot_id=snapshot_id,
    )
    monkeypatch.setattr(service, "_dataset", lambda _: changed)
    with pytest.raises(ValueError, match="output limit"):
        service.describe_snapshot(snapshot_id)


def test_table_extension_does_not_change_old_query_serialization():
    assert DatasetQuerySpec().model_dump()["table"] is None
    assert "table" not in QuerySpec().model_dump()
    with pytest.raises(ValidationError):
        QuerySpec(table="source")


def native_scalar_cases():
    """Every scalar family accepted by records imports, through retained snapshots."""
    cases = []
    for bits in (8, 16, 32, 64):
        for family in ("int", "uint"):
            dtype = getattr(pa, f"{family}{bits}")()
            cases.append(pytest.param(dtype, [3, None, 1], 3, 3, 1, id=str(dtype)))
    for bits in (16, 32, 64):
        dtype = getattr(pa, f"float{bits}")()
        cases.append(pytest.param(dtype, [1.5, None, -2.0], 1.5, 1.5, -2.0, id=str(dtype)))
    cases.append(pytest.param(pa.bool_(), [True, None, False], True, True, False, id="bool"))
    for dtype in (pa.string(), pa.large_string()):
        cases.append(pytest.param(dtype, ["003", None, "001"], "003", "003", "001", id=str(dtype)))
    for bits, precision in ((32, 7), (64, 15), (128, 30), (256, 60)):
        dtype = getattr(pa, f"decimal{bits}")(precision, 2)
        cases.append(
            pytest.param(
                dtype,
                [Decimal("1.50"), None, Decimal("-2.00")],
                "1.50",
                "1.50",
                "-2.00",
                id=str(dtype),
            )
        )
    for dtype in (pa.date32(), pa.date64()):
        cases.append(
            pytest.param(
                dtype,
                [date(2026, 1, 2), None, date(2026, 1, 1)],
                "2026-01-02",
                "2026-01-02",
                "2026-01-01",
                id=str(dtype),
            )
        )
    for unit, factor in (("s", 1), ("ms", 1000), ("us", 10**6), ("ns", 10**9)):
        dtype = pa.time32(unit) if unit in ("s", "ms") else pa.time64(unit)
        # Parquet may encode second-resolution input as milliseconds; expected
        # strings use the retained descriptor's explicit precision below.
        cases.append(
            pytest.param(
                dtype,
                [43_200 * factor, None, 3600 * factor],
                "12:00:00",
                "12:00:00",
                "01:00:00",
                id=str(dtype),
            )
        )
        for timezone in (None, "Europe/London"):
            dtype = pa.timestamp(unit, timezone)
            high = datetime(2026, 7, 2, tzinfo=UTC)
            low = datetime(2026, 7, 1, tzinfo=UTC)
            high_json = "2026-07-02T01:00:00+01:00" if timezone else "2026-07-02T00:00:00"
            low_json = "2026-07-01T01:00:00+01:00" if timezone else "2026-07-01T00:00:00"
            cases.append(
                pytest.param(
                    dtype, [high, None, low], high_json, high_json, low_json, id=str(dtype)
                )
            )
    return cases


@pytest.mark.parametrize(
    ("dtype", "values", "operand", "high_json", "low_json"), native_scalar_cases()
)
def test_all_native_scalar_families_query_filter_sort_paginate_and_identify(
    tmp_path, monkeypatch, dtype, values, operand, high_json, low_json
):
    source = tmp_path / "invented-native.parquet"
    pq.write_table(pa.table({"id": ["a", "b", "c"], "value": pa.array(values, type=dtype)}), source)
    store = Store(tmp_path / "store")
    dataset = store.import_dataset(
        source,
        {
            "dataset_id": "invented-native",
            "title": "Invented native scalar fixture",
            "columns": {"value": {"role": "identifier"}},
        },
    )
    original = dataset.to_arrow()
    hashes = store.verify(dataset.snapshot_id)["files"]
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    service = Service(store.root, index_store=False)
    retained_type = original["value"].type
    if pa.types.is_time(retained_type):
        digits = {"s": 0, "ms": 3, "us": 6, "ns": 9}[retained_type.unit]
        fraction = "." + "0" * digits if digits else ""
        high_json += fraction
        low_json += fraction
    first = service.query_dataset(dataset.snapshot_id, {"sort": [{"column": "value"}], "limit": 1})
    second = service.query_dataset(
        dataset.snapshot_id, {"sort": [{"column": "value"}], "limit": 1, "offset": 1}
    )
    assert first["rows"] == [{"id": "c", "value": low_json}]
    assert second["rows"] == [{"id": "a", "value": high_json}]
    assert first["next_offset"] == 1 and second["next_offset"] == 2
    filtered = service.query_dataset(
        dataset.snapshot_id, {"filters": [{"column": "value", "value": operand}]}
    )
    assert filtered["rows"] == [{"id": "a", "value": high_json}]
    from ssdu.dataset_contract import arrow_dtype

    assert filtered["column_types"]["value"] == arrow_dtype(retained_type)
    dimensions = service.list_snapshot_dimension_values(dataset.snapshot_id, "value", limit=1)
    assert dimensions["items"][0]["value"] == low_json
    assert dimensions["total"] == 3 and dimensions["next_offset"] == 1
    assert store.open_dataset(dataset.snapshot_id).to_arrow().equals(original)
    assert store.verify(dataset.snapshot_id)["files"] == hashes


@pytest.mark.parametrize(
    ("condition", "count"),
    [
        ({"value": None}, 3),
        ({"value": 1}, 0),
        ({"value": True}, 0),
        ({"value": "text"}, 0),
        ({"op": "gte", "value": 1}, 0),
        ({"op": "lte", "value": "text"}, 0),
        ({"op": "in", "value": [1, None, True]}, 3),
        ({"op": "in", "value": [1, True]}, 0),
    ],
)
def test_untyped_null_columns_have_explicit_match_semantics(tmp_path, condition, count):
    source = tmp_path / "invented-nulls.parquet"
    pq.write_table(pa.table({"value": pa.nulls(3)}), source)
    store = Store(tmp_path / "store")
    dataset = store.import_dataset(
        source, {"dataset_id": "invented-nulls", "title": "Invented null records"}
    )
    service = Service(store.root, index_store=False)
    result = service.query_dataset(
        dataset.snapshot_id, {"filters": [{"column": "value", **condition}]}
    )
    assert result["rows"] == [{"value": None}] * count
    assert result["column_types"]["value"] == "null"
    with pytest.raises(ValueError, match="Only equality"):
        service.query_dataset(
            dataset.snapshot_id, {"filters": [{"column": "value", "op": "gte", "value": None}]}
        )


@pytest.mark.parametrize("dtype", [pa.float16(), pa.decimal32(7, 2), pa.decimal64(15, 2)])
def test_exact_execution_widening_does_not_accept_boolean_numeric_operands(tmp_path, dtype):
    value = 1.5 if pa.types.is_floating(dtype) else Decimal("1.50")
    source = tmp_path / "invented-narrow.parquet"
    pq.write_table(pa.table({"value": pa.array([value, None], type=dtype)}), source)
    store = Store(tmp_path / "store")
    dataset = store.import_dataset(
        source, {"dataset_id": "invented-narrow", "title": "Invented narrow scalar records"}
    )
    with pytest.raises(ValueError, match="filters require"):
        Service(store.root, index_store=False).query_dataset(
            dataset.snapshot_id, {"filters": [{"column": "value", "value": True}]}
        )
