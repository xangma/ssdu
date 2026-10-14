# SSDU: UK social-science data in one place

SSDU helps you download supported UK social-science datasets, import your own
tables and keep a reliable copy of the data used in an analysis. You can return
to that copy later or share a saved collection with a colleague, subject to the
source's terms.

You work in Python or through the command line. There is no point-and-click data
portal yet. If you have used a little Python, the guided tutorial will take you
through a small task one step at a time.

## Start here

1. [Read the guided tutorial](start/research-tutorial.md): inspect an invented
   community-centre table, select areas with at least three centres in 2024 and
   reopen your saved data. You can read its results without installing anything.
2. [Install SSDU](start/install.md) to try the notebook on your computer.
3. [Check supported sources](reference/sources.md) before choosing a real dataset.

The tutorial uses invented values, so you can learn without a provider account
or downloading official data.

## When you are ready

- **Try different sources:** the [provider examples](guides/dataset-examples.md)
  explain how Nomis, StatsWales and English deprivation tables are represented.
- **Work with your own table:** the [dataset API guide](guides/dataset-api.md)
  covers local CSV and Parquet imports, inspection and selection.
- **Move a saved analysis:** the [offline transfer example](start/quickstart.md)
  demonstrates a complete collection transfer.
- **Check what the figures mean:** read about
  [sources and interpretation](concepts/provenance.md), including dates, units
  and geographical boundaries.

For application developers, the [interface guide](guides/interfaces.md) links to
Python, command-line, REST and MCP references. The
[dataset structure](concepts/dataset-contract.md) describes the common table and
metadata format in detail.

## What is covered?

SSDU supports selected datasets and formats from Nomis, StatsWales, English
deprivation statistics, NISRA and Scottish linked data, plus local CSV and
Parquet tables. The [coverage matrix](reference/sources.md) describes the exact
support and limits; the [four-nation example](guides/four-nations.md) shows how
separate national datasets can be saved in one collection.

A common format does not make measures comparable. Check definitions, dates,
units and geographical boundaries before combining data. Search currently uses
catalogue descriptions saved locally, which may be incomplete or out of date.
[Discovery research](research/index.md) explores other search methods separately.

```{toctree}
:maxdepth: 2
:caption: Start
:hidden:

start/install
start/research-tutorial
start/quickstart
```

```{toctree}
:maxdepth: 2
:caption: Guides
:hidden:

guides/dataset-examples
guides/dataset-api
guides/four-nations
guides/interfaces
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
