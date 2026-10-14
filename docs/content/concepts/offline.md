# Work with saved data offline

Once data have been downloaded or imported, you can reuse the saved copy without
contacting the provider. This helps you continue work without a connection and
return to the exact data behind an earlier result.

SSDU separates reading saved data from downloading new data. Local imports, snapshot replay,
collection transfer and the default REST/MCP read interfaces operate on local
files. They do not fetch missing provider data as a side effect.

## Retained data and freshness

A normal fetch first uses a verified snapshot for the exact request. A cache hit
does not check the provider for updates. Use `--refresh` deliberately when you
want a new provider acquisition.

For commands that support it, SSDU's `--offline` requires retained inputs and
forbids provider requests. Missing inputs cause an error; they are not silently
downloaded. `--offline` and `--refresh` cannot be combined.

| Operation | Source of information |
|---|---|
| `registry search` | Retained local catalogue metadata; coverage may be partial or stale. |
| `search` | Live provider search; deprivation search uses curated descriptions. |
| `sync` | Retained snapshots indexed locally; no provider catalogue harvest. |
| `harvest` | Explicit metadata acquisition under the selected source's policy. |
| `fetch --offline` | Verified retained data for the exact request. |
| `collection replay` | Pinned local snapshots, metadata and queries. |
| Default REST/MCP reads | Local registry and frozen snapshots. |

Consult the [CLI reference](../reference/cli.md) for each command's flags.
`demo --offline` needs a previously retained demo; it is not a first-run tutorial.
Start with the [guided tutorial](../start/research-tutorial.md) to learn using
invented local data, or try the [transfer example](../start/quickstart.md) to move
a saved collection.

## Three different boundaries

`uv run --offline` stops `uv` from downloading dependencies. It does not constrain
the application launched by `uv`.

SSDU's applicable `--offline` options constrain provider acquisition. Local-only
operations require no acquisition flag. Optional retrieval controls can plan and
queue work locally; a separate explicitly run worker performs acquisition.

An operating-system network block or a socket/DNS guard is a stronger boundary
when checking that a workflow makes no network attempts. Install dependencies
before imposing that boundary. A successful dependency-offline invocation alone
does not prove an application made no requests.

## Retain what replay needs

Transfer a collection bundle rather than a lock file alone: the bundle carries
its retained snapshots and pinned metadata. Keep the installed SSDU code and
dependencies available too; a data bundle is not a Python runtime.

Frozen discovery benchmarks can replay retained vectors without running an
embedding model. Regenerating vectors or acquiring missing evidence has different
requirements; follow the specific [research workflow](../research/index.md).
