# Find, select and save a provider dataset

Suppose you find a useful table today and want a colleague to reopen the same
figures next month. SSDU's `Connector` takes you from a provider's catalogue to a
saved dataset. You can inspect its description before downloading values, then
keep the result in a collection that can travel to another computer.

The common workflow is available for **Nomis, StatsWales, MHCLG, NISRA and
Scotland**. Their selection codes and supported formats differ; check the
[coverage matrix](../reference/sources.md) before choosing a source.

To collect descriptions for discovery before choosing a table, see
[keeping a searchable metadata catalogue](metadata-sync.md).

## Try the complete example

After [installation](../start/install.md), run from the project folder:

```sh
uv run --frozen --offline python examples/provider_workflow.py --output-dir data/provider-workflow
```

Choose a new or empty output folder. Every response and value in this example
is **invented teaching material**, supplied in memory. No provider is contacted.
The script blocks network connections, so an accidental live request fails.

The example finds two fictional MHCLG publications: housing counts and local
budgets. It uses the same connector for both, although their columns differ:

1. Browse the catalogue and read each publication's description.
2. Choose a CSV attachment and select a row using one of its columns.
3. Inspect the retrieval plan and save a snapshot of the selected table.
4. Read the retained catalogue and descriptions offline.
5. Save both snapshots in a collection and export a bundle.
6. Move the original store aside, import the bundle into a fresh store and
   verify that the saved rows and collection results still match.

Look for `"status": "passed"` and `"provider_network_requests": 0` in the
printed result. The selected housing row has area `A01` and 12 dwellings; the
selected budget row has authority `A02` and an invented budget of 95000.
These are unrelated teaching tables, not comparable official indicators.

The output folder contains `result.json` and
`invented-publications.ssdu.zip`. The ZIP contains the files needed to reopen
the collection. A lockfile alone identifies those files but does not carry them.

## Use the same steps with a provider

Create a connector using `"nomis"`, `"statswales"`, `"mhclg"`, `"nisra"` or
`"scotland"`:

```python
from ssdu import Connector, Request

provider = Connector("nomis", root="my-data")
page = provider.list_datasets("employment", limit=20)
print(page)
```

These calls can contact the provider when a response is not already cached.
Catalogue results are paged: use `limit` and `offset` to request another page.
Choose a dataset ID from the results, then inspect it:

```python
description = provider.describe_dataset(dataset_id)
print(description)
```

For an API table, use `list_dimension_values(dataset_id, dimension)` to inspect
selection codes such as areas or years. A dimension is a category that
describes the figures, such as age group. For MHCLG file publications, choose
an attachment from the description instead. File-column selections happen
locally after the attachment is downloaded.

Once you have chosen the source's codes or attachment, put them in a `Request`.
The methods for saving and reopening the result are the same:

```python
request = Request(provider.provider, dataset_id, params=selection)
plan = provider.plan_fetch(request)
print(plan)
dataset = provider.fetch_dataset(request)
print(dataset.to_pandas())

# Reopen that request's retained snapshot without contacting the provider.
same_dataset = provider.fetch_dataset(request, offline=True)
```

`dataset_id` and `selection` above come from the metadata you inspected; the
complete teaching scripts supply concrete examples. Nomis, StatsWales, NISRA
and Scotland can return statistical observations with dimensions and measures.
An ordinary MHCLG CSV is retained as records, preserving its column names and source text.
SSDU does not guess units or make different tables comparable.

## Try Northern Ireland and Scotland

This second example follows the same steps for two invented tables: workshop
attendance in Northern Ireland and community events in Scotland. The measures
are unrelated; putting them in one collection does not make them comparable.

```sh
uv run --frozen --offline python examples/national_connectors.py --output-dir data/national-connectors
```

It browses each catalogue, reads the dataset description and available years,
selects one year, inspects the plan and saves a dataset. It then checks that
the catalogue, description and year codes reopen offline. Finally it exports
both datasets as one collection, moves the original store aside and verifies
the collection in a fresh store with HTTP and network connections blocked.
All responses are invented in memory; no provider is contacted.

Look for `"status": "passed"`, `"provider_network_requests": 0` and
`"fresh_store_replayed_offline": true`. The output includes `result.json` and
`invented-national-connectors.ssdu.zip`. You can also run the
{download}`self-contained script <../_downloads/national_connectors.py>`
from another folder after installing SSDU.

The method names stay the same, while the provider's codes are preserved:

| Provider | Example year dimension | Example selection |
| --- | --- | --- |
| NISRA | `TLIST(A1)` | `{"selectors": {"TLIST(A1)": ["2024"]}}` |
| Scotland | `http://purl.org/linked-data/sdmx/2009/dimension#refPeriod` | `{"dimensions": {dimension_uri: [year_uri]}}` |

For Scotland, the example year URI is
`http://reference.data.gov.uk/id/year/2024`. Use codes returned by
`list_dimension_values`; do not replace them with labels from a chart. Scottish
code lists contain members observed in that dataset, rather than every possible
member of an external geography or time vocabulary.

NISRA currently retrieves the complete English-language cube before selecting
rows locally, with a limit of 100,000 cells. A small selection therefore cannot
make an oversized cube downloadable. Scotland supports linked-data observation
cubes using bounded, selected queries; file/ZIP publications and slice-attached
components are not yet supported. The dataset description reports these limits
before you retrieve values.

These services do not offer historical release selection through this workflow.
SSDU checks metadata during acquisition and retains the result; use that saved
snapshot or a collection lock to reproduce it later.

## Keep the version you used

Repeated requests normally reuse the saved result. Use `refresh=True` when you
deliberately want to check the provider again; this can create a new snapshot.
Use the saved snapshot or collection lock to return to the exact version used
in an analysis.

Metadata that you have already fetched can also be inspected with
`offline=True`. A transferred collection carries the snapshots and source
information needed for replay; it does not promise to include every catalogue
search page you previously browsed. See
[offline transfer](../start/quickstart.md) and
[sources and interpretation](../concepts/provenance.md).

REST and MCP summarise large dataset descriptions. Browse the available codes
a page at a time with `list_provider_dimension_values`; Python can also inspect
the complete cached metadata.

## Read the complete script

The self-contained script is available as a
{download}`download <../_downloads/provider_workflow.py>`.

```{literalinclude} ../_downloads/provider_workflow.py
:language: python
```
