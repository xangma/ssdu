# Connect to retained data

SSDU's Python, CLI, REST and MCP interfaces share local snapshots and catalogue
metadata. Start with a populated store, such as one produced by the
[quickstart](../start/quickstart.md). An empty store can return an empty catalogue;
it does not cause a provider harvest.

## Python and CLI

Use `Store` for retained snapshots, `Collections` for pinned replay and transfer,
and `Service` for bounded catalogue and query operations. Selected imports and
signatures are generated in the [Python reference](../reference/python.md).

For the standardized [dataset contract](../concepts/dataset-contract.md), use
`Store.fetch_dataset()` to acquire or reuse supported provider data and
`Store.open_dataset()` to verify a retained dataset. Arrow and pandas access
default to observations, with a separate retained source table. Follow the
[provider examples](dataset-examples.md), and check the generated
[source coverage](../reference/sources.md) before acquisition. Local imports
retain their existing table interface and do not yet use the observation profile.

The CLI's global `--store` option precedes its subcommand. For example, inspect
the transferred quickstart store with:

```sh
uv run --frozen --offline ssdu --store data/quickstart/fresh registry search
```

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

Package and transport versions are reported separately: package `0.3.0`, REST/MCP
`0.2.0`. These labels should not be treated as a single release number.
