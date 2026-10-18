# Connect to retained data

This guide is for readers connecting SSDU to scripts, applications or AI tools.
You can use Python, the command line (CLI), a web API (REST), or an AI-tool
connection (MCP). All read the same saved datasets and local catalogue.

Start with data already saved in a store, such as one produced by the
[offline transfer example](../start/quickstart.md). An empty store can return an empty catalogue;
it does not cause a provider harvest.

## Python and CLI

Use `Store` for retained snapshots, `Collections` for pinned replay and transfer,
and `Service` for bounded catalogue and query operations. Selected imports and
signatures are generated in the [Python reference](../reference/python.md).

For the standardized [dataset contract](../concepts/dataset-contract.md), use
`Store.fetch_dataset()` to acquire or reuse supported provider data and
`Store.open_dataset()` to verify a retained dataset. `Store.import_dataset()`
returns a dataset from a local CSV/Parquet file and `LocalImportSpec`. Statistical
observations use the observation profile. Local imports and general MHCLG CSV
publications use the records profile, which preserves fields and describes row
grain, declared keys and field meanings where known. Follow the
[dataset API guide](dataset-api.md) to describe, query and inspect dimensions
across interfaces. The [provider examples](dataset-examples.md) explain
observation semantics; check [source coverage](../reference/sources.md) before
acquisition. Dataset reads require a retained contract; they do not acquire data
or migrate historical snapshots.

The CLI's global `--store` option precedes its subcommand. For example, inspect
the transferred quickstart store with:

```sh
uv run --frozen --offline ssdu --store data/quickstart/fresh registry search
```

`registry search` finds datasets already saved as data snapshots. To search
provider descriptions captured before downloading data, use `metadata search`
and the [metadata discovery tutorial](../start/metadata-discovery.md). These are
separate catalogues; an empty result in one does not describe the other.

See the [CLI reference](../reference/cli.md) for all groups and nested options.

## REST

Start the default local read service:

```sh
uv run --frozen --offline ssdu --store data/quickstart/fresh serve
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation, or inspect
`http://127.0.0.1:8000/openapi.json`. Stop the server with `Ctrl+C`.

The generated [REST reference](../reference/rest.md) includes the exported OpenAPI
contract. Search and describe datasets before choosing a snapshot to preview or
query. The provenance operation supplies source and verification information.

## MCP

Launch the stdio server as your MCP client's command, using the same store:

```sh
uv run --frozen --offline ssdu --store data/quickstart/fresh mcp
```

For a client supporting Streamable HTTP, run:

```sh
uv run --frozen --offline ssdu --store data/quickstart/fresh mcp --transport streamable-http
```

Its default endpoint is `http://127.0.0.1:8001/mcp`. The generated
[MCP reference](../reference/mcp.md) lists tool arguments, outputs and resource
templates. Treat provider descriptions as source material, and cite returned
provenance when using data in an answer.

## Limits and optional retrieval controls

Default REST/MCP operations read retained data without provider requests. Adding
`--enable-jobs` enables local plan and job controls; submitting a job queues work.
Acquisition requires a separate worker explicitly run through the CLI. No worker
runs inside either server.

Queries use validated columns, filters, sorting and pagination rather than
arbitrary SQL. The [schema reference](../reference/schemas.md) describes typed
limits; the transport references document additional request/response bounds.
Missing or unverifiable snapshots produce errors.
