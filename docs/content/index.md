# SSDU

SSDU helps you discover, retain and reuse UK social-sciences data. It combines
local CSV/Parquet imports, adapters for Nomis, StatsWales, English deprivation,
NISRA portal JSON-stat cubes and scoped Scottish linked-data cubes,
a metadata registry, immutable snapshots and portable collection locks.

Use it to preserve the data and interpretation behind an analysis, transfer a
collection to another machine, or expose retained data through Python, REST or
MCP. Researchers and policy teams can inspect source evidence before selecting
data; other readers can explore catalogue descriptions without acquiring values.

Start with the [offline quickstart](start/quickstart.md). It creates a small
synthetic table and transfers a locked query between fresh stores. No provider
account, dataset download or existing cache is required.

## Choose your next step

- [Install SSDU](start/install.md) from the repository's locked environment.
- [Understand offline operation](concepts/offline.md) and explicit acquisition.
- [Inspect provenance](concepts/provenance.md) before comparing or citing results.
- [Use the dataset contract](concepts/dataset-contract.md) for observations and typed local records.
- [Inspect and query retained datasets](guides/dataset-api.md) through a common interface.
- [Run provider examples](guides/dataset-examples.md) with synthetic offline responses.
- [Transfer four distinct national datasets](guides/four-nations.md) with resumable jobs.
- [Connect an interface](guides/interfaces.md) to retained metadata and snapshots.
- Check [source coverage](reference/sources.md) for implemented support and limits.
- Inspect [family conformance](reference/conformance.md) for validated shapes.
- Consult the generated [CLI](reference/cli.md), [Python](reference/python.md),
  [schemas](reference/schemas.md), [REST](reference/rest.md) and [MCP](reference/mcp.md)
  references for supported arguments and contracts.

## Scope and interpretation

The registry describes what SSDU has retained. Its coverage can be incomplete or
stale, and a catalogue match does not establish that observations are available
or suitable for your question. Check measure definitions, release, observation
period, geography, access conditions and missing information.

[Discovery research](research/index.md) compares experimental ranking methods
on frozen evidence. Those benchmarks are separate from the production registry
and API search. Their latest label overlay is agent-adjudicated; human validation
remains pending.

```{toctree}
:maxdepth: 2
:caption: Start
:hidden:

start/install
start/quickstart
```

```{toctree}
:maxdepth: 2
:caption: Guides
:hidden:

guides/interfaces
guides/dataset-api
guides/dataset-examples
guides/four-nations
```

```{toctree}
:maxdepth: 2
:caption: Concepts
:hidden:

concepts/offline
concepts/provenance
concepts/dataset-contract
```

```{toctree}
:maxdepth: 2
:caption: Reference
:hidden:

reference/cli
reference/python
reference/schemas
reference/rest
reference/mcp
reference/sources
reference/conformance
```

```{toctree}
:maxdepth: 2
:caption: Examples
:hidden:

examples/nomis
examples/statswales
examples/deprivation
```

```{toctree}
:maxdepth: 2
:caption: Research
:hidden:

research/index
```
