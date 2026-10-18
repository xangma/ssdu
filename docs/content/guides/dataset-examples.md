# Learn how different sources represent data

Sources can describe similar-looking figures in different ways. These examples
show how SSDU presents their tables through one Python interface while keeping
important differences visible: missing values, units, dates and area codes.

Each notebook uses **invented teaching responses, not official statistics**.
You can read the displayed results without installing anything. To learn the
basic workflow first, follow the [guided tutorial](../start/research-tutorial.md).

## Choose an example

| Example | What you will learn |
|---|---|
| [Nomis](../examples/nomis.md) | Keep area codes intact, and distinguish zero from a withheld or missing value. |
| [StatsWales](../examples/statswales.md) | Select by area and year, and retain notes and missing-value meanings. |
| [English deprivation (legacy format example)](../examples/deprivation.md) | Read several measures from one source table without confusing a rank, score or count. |

For a new provider request, start with the [provider selection tutorial](../start/provider-selection.md).
It explains how to choose an ID and selection codes. General MHCLG integration
uses publication IDs and supported attachments; the deprivation notebook below
retains the older dataset-specific adapter as a format and compatibility example.

The same `Store`, `Request` and `Dataset` objects appear in each example. A
`Request` describes the selection; the `Store` saves it; the `Dataset` gives
access to its tables and description. The
[dataset structure](../concepts/dataset-contract.md) explains these tables in more
detail.

The [four-nation example](four-nations.md) adds separate Scottish and Northern
Irish datasets to a collection. It shows how to keep sources together, rather
than treating them as comparable national indicators.

## Run a provider notebook yourself

After [installation](../start/install.md), open a notebook from the `ssdu` project
folder. For Nomis, run:

```sh
uv run --frozen --offline --extra notebook jupyter lab examples/notebooks/nomis.ipynb
```

Use the same command with `statswales.ipynb` or `deprivation.ipynb` for the other
examples. Run cells in order with **Shift+Enter**. Keep the notebooks inside the
project: they use the included example helper and invented response files.

## Save and transfer all three examples

To try the combined workflow, run:

```sh
uv run --frozen --offline python examples/dataset_contract.py --output-dir data/dataset-contract-example
```

Choose an output folder that does not exist. The script saves all three
datasets, groups them into a collection and transfers it to a fresh store. It
then reopens the tables without network access. Look for `verified` and
`fresh_store_equal` set to `true` in the result.

## Before choosing real data

Check the [source coverage matrix](../reference/sources.md): these examples cover
specific formats, rather than every table from each provider. The teaching
requests and invented responses are not a ready-made research dataset.

For a live selection, first inspect the provider's dataset description and
available area, date and measure codes. A normal SSDU client downloads the
selection when it is not already cached; the notebooks use a special teaching
client so no real provider is contacted. Do not treat their invented values as
live results.

Keep the source release, units, missing-value meanings and geographical
boundaries with your result. Subsequent reads can use the saved copy; see
[working offline](../concepts/offline.md). The
[dataset API guide](dataset-api.md) covers inspection and selection in Python
and other interfaces.
