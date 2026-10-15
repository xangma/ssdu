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
through StatsWales, the **Scottish Government** through statistics.gov.scot,
**NISRA** through its Data Portal, and selected **MHCLG** statistics on area
deprivation in England. The
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

- [Work with provider examples](https://xangma.github.io/ssdu/guides/dataset-examples.html)
  using invented responses in the same formats as supported sources.
- [Inspect and query datasets](https://xangma.github.io/ssdu/guides/dataset-api.html)
  through Python, the command line or an application.
- [Understand sources and interpretation](https://xangma.github.io/ssdu/concepts/provenance.html)
  before citing or combining results.

The software and documentation use the [MIT licence](LICENSE). Real datasets
retain their providers' terms. This repository publishes code and invented
teaching data; it does not distribute provider datasets.

For contributors, [documentation maintenance](docs/README.md) explains local
builds and checks, and the [publication policy](PUBLICATION.md) explains the
source-only release.
