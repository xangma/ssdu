# Inspect and query a retained dataset

The dataset API exposes one retained snapshot through Python, CLI, REST and MCP.
Choose a snapshot, inspect its description, select a table and query it with
bounded filters and pagination. These operations read retained evidence without
acquiring or migrating data. A historical snapshot without a contract requires
explicit [migration](../concepts/dataset-contract.md#open-historical-snapshots).

## Choose the table and interpretation

The [dataset contract](../concepts/dataset-contract.md) describes the meaning of
the retained tables. The `observations` profile remains compatible with contract
`1.0.0`. Contract `1.1.0` adds a `records` profile for local CSV/Parquet imports:
it preserves field types and describes declared keys, row grain (what one row
represents), field meanings, units and null semantics.

Inspect the descriptor to learn which tables are available. Use `observations`
for normalized provider observations, `records` for typed local records, or
`source` for the retained source table. A common query interface does not make
different measures, units or row grains comparable. The
[provider examples](dataset-examples.md) show how raw values, suppression and
geography vintages affect interpretation.

## Import local records in Python

`Store.import_dataset(path, spec)` returns a `Dataset`. For a CSV named
`services.csv` with unique `unit_code` strings and integer `count` values:

```python
from ssdu import DatasetQuerySpec, LocalImportSpec, Service, Store

store = Store("data/local-records")
spec = LocalImportSpec(
    dataset_id="services",
    title="Local service counts",
    primary_key=["unit_code"],
    row_grain="One row per unit.",
    columns={
        "unit_code": {"type": "string", "definition": "Source unit identifier"},
        "count": {"type": "int64", "definition": "Number of services", "unit": "services"},
    },
)
dataset = store.import_dataset("services.csv", spec)
service = Service(store.root)
description = service.describe_snapshot(dataset.snapshot_id)
page = service.query_dataset(dataset.snapshot_id, DatasetQuerySpec(limit=20))
```

CSV fields default to strings. Declare numeric types explicitly to preserve
identifiers such as `001`. `null_values` controls which exact CSV tokens become
null; its default is the empty string. Parquet retains native scalar types and
nulls. Column `missing_values` explains missing-value meanings; it does not
convert tokens to null. Declared primary keys must be unique and non-null.

Omitting the query table selects the profile: `records` here, or `observations`
for a provider dataset. `Dataset.to_arrow()` and `to_pandas()` follow the same
default. The legacy `import_local()` function continues to return a manifest.

## Follow the same workflow across interfaces

Python callers use the shared `Service` operations `describe_snapshot`,
`query_dataset` and `list_snapshot_dimension_values`. `query_dataset` accepts a
`DatasetQuerySpec`, with an optional table selection. The CLI exposes the same
workflow through `dataset describe`, `dataset query` and `dataset dimensions`.

REST exposes the snapshot descriptor at `GET /snapshots/{snapshot_id}/dataset`,
queries at `POST /snapshots/{snapshot_id}/dataset/query`, and dimension values at
`GET /snapshots/{snapshot_id}/dataset/dimensions/{dimension}`. MCP exposes the
same three method names as read-only tools and the descriptor resource
`ssdu://snapshots/{snapshot_id}/dataset`.

Use the generated [Python](../reference/python.md), [CLI](../reference/cli.md),
[schema](../reference/schemas.md), [REST](../reference/rest.md) and
[MCP](../reference/mcp.md) references for exact arguments, supported filters,
limits and response fields.

1. Describe the snapshot and review its profile, table grain, keys, field
   definitions, units and null meanings.
2. Inspect a dimension's observed values when choosing a filter. Follow its
   pagination to read more codes.
3. Query the selected table with a bounded page size. Keep the snapshot identifier
   and table selection with the result.
4. Review [provenance](../concepts/provenance.md) before comparing or citing data.

Descriptor inspection is bounded: dimension code arrays are omitted, with
`code_count` and `codes_omitted` making that omission explicit. Use the dimension
operation to page through observed codes. Those codes describe the retained
selection; they do not establish complete provider coverage. For records, this
operation accepts declared primary-key fields or fields with role `identifier`.

## Read typed query results

JSON query results include `column_types` and `value_encoding`. Decimals are
exact strings; dates, times and timestamps are ISO strings retaining precision
and timestamp offsets. Integers are JSON integers: consumers must preserve the
declared integer width, including values beyond JavaScript's safe integer range.
Keep those hints, units, quality and provenance with the rows.

Queries return at most 100 rows and 100 selected columns, with a 256 KB output
limit. Dataset reads admit at most five million rows per table and 256 MB summed
across table sizes, checked against Parquet uncompressed sizes before loading
and decoded Arrow sizes afterwards. These are input limits, not peak memory
guarantees. Query execution has a five-second cooperative budget checked between
table operations; an operation already running is not interrupted. File-hash and
semantic verification precede that timer, so it is not an end-to-end timeout.

## Transfer and replay

After [installation](../start/install.md), run from the repository root with a
new output directory:

```sh
uv run --frozen --offline python examples/dataset_api.py --output-dir data/dataset-api-example
```

The {download}`complete script <../_downloads/dataset_api.py>` uses this checkout's
example helpers and invented fixtures. Keep them together when running it.

The example uses invented Nomis responses, a local CSV and a Parquet file created
at runtime. It checks Python, actual CLI, REST and actual MCP results, then
transfers a collection bundle and repeats the reads in a fresh offline process.
Original input and store paths are moved aside, and access to them and networking
is denied during replay. A successful run reports `verified` and
`fresh_store_verified` as `true`, 90 `api_parity_comparisons`, and
`provider_network_requests` and `network_attempts` as `0` in `result.json`.
These are teaching data, not published statistics; see the
[provider examples](dataset-examples.md) for interpretation.

Existing `query_data` and `QuerySpec` remain available. Historical collection
lock serialization is unchanged, so adopting the dataset API does not require
rewriting existing locks. See [offline operation](../concepts/offline.md) for
retained replay and explicit acquisition.
