# SSDU

SSDU helps you bring together UK social-science data for research and policy
work. Use one Python interface to download supported datasets or import your own
tables, keep track of where the data came from, and save the version used in an
analysis so you can reproduce it later.

**[Read the manual](https://xangma.github.io/ssdu/)** ·
**[Try the guided tutorial](https://xangma.github.io/ssdu/start/research-tutorial.html)** ·
**[Check provider coverage](https://xangma.github.io/ssdu/reference/sources.html)**

## What can I do with it?

- Download a supported dataset, then reuse the saved copy without downloading it
  again. SSDU caches requests and respects configured provider rate limits.
- Import local CSV or Parquet tables and describe their columns, units and source.
- Inspect and select data through a common interface, while keeping each source's
  definitions and missing-value meanings.
- Save the data and selection behind an analysis, or transfer them to another
  machine for use offline.
- Search descriptions already held in your local catalogue. SSDU also provides
  interfaces for applications and AI tools through REST and MCP.
- Retain available variables, explicit questions and response codes from supported
  MetadataWorks services, with source references and offline transfer.
- Build that catalogue from provider metadata, with resumable refresh jobs,
  coverage reports and saved versions you can search offline.

SSDU is a Python package with command-line tools. You will need some Python or
terminal experience; it does not currently provide a point-and-click data portal.
The tutorial explains each step and uses a notebook, where you can run short
pieces of Python one at a time.

## Start with a small research task

The [guided tutorial](https://xangma.github.io/ssdu/start/research-tutorial.html)
asks: **which areas had at least three community centres in 2024?** You will
inspect an invented table, select rows and reopen the same saved data. The
fictional areas make it safe to explore without confusing the results with
official statistics.

All tutorial values are invented and clearly labelled. You do not need a provider
account or a real dataset download. Follow the
[installation guide](https://xangma.github.io/ssdu/start/install.html), or, if Git
and [uv](https://docs.astral.sh/uv/getting-started/installation/) are already
installed, run:

```sh
git clone https://github.com/xangma/ssdu.git
cd ssdu
uv sync --frozen --python 3.13 --extra notebook
uv run --frozen --offline --extra notebook jupyter lab examples/notebooks/research_tutorial.ipynb
```

The first installation needs an internet connection. The tutorial itself uses
local, synthetic data.

## Which data providers are covered?

SSDU has partial coverage of **ONS** through Nomis, the **Welsh Government**
through StatsWales, **NISRA** through its Data Portal, and selected **MHCLG**
statistical publications. For the **Scottish Government**, the current
`data.gov.scot` portal supports metadata discovery; data retrieval uses the
separate legacy linked-data integration. The
[provider coverage matrix](https://xangma.github.io/ssdu/reference/sources.html)
lists the supported datasets, access routes, limits and planned integrations.
Support for one dataset or service does not mean all of a provider's data are
available. Local CSV and Parquet imports are supported separately.

A common interface helps you work with different sources; it does not make their
statistics directly comparable. Check definitions, units, dates and geographical
boundaries before combining results. Local catalogue search covers descriptions
you have retained, rather than every UK dataset. Semantic and hybrid search are
currently [research work](https://xangma.github.io/ssdu/research/index.html).

## Go further

- [Choose a provider table and selection](https://xangma.github.io/ssdu/start/provider-selection.html):
  browse a teaching catalogue, choose areas and years, then save the result.
- [Search a metadata catalogue](https://xangma.github.io/ssdu/start/metadata-discovery.html):
  find descriptions and check coverage, freshness and download support.
- [Import a CSV or Parquet file](https://xangma.github.io/ssdu/start/local-files.html):
  describe columns and missing values, select rows and reopen the saved copy.
- [Understand sources and interpretation](https://xangma.github.io/ssdu/concepts/provenance.html)
  before citing or combining results.

These lessons are executable and use synthetic inputs. The manual also covers
[larger metadata captures](https://xangma.github.io/ssdu/guides/metadata-sync.html),
[deep metadata and code lists](https://xangma.github.io/ssdu/guides/metadata-graph.html),
and [the shared application interfaces](https://xangma.github.io/ssdu/guides/interfaces.html).

The software and documentation use the [MIT licence](LICENSE). Real datasets
retain their providers' terms. This repository publishes code and invented
teaching data; it does not distribute provider datasets.

For contributors, [documentation maintenance](docs/README.md) explains local
builds and checks, and the [publication policy](PUBLICATION.md) explains the
source-only release.
