# Find, select and save a provider dataset

Suppose you find a useful table today and want a colleague to reopen the same
figures next month. SSDU's `Connector` takes you from a provider's catalogue to a
saved dataset. You can inspect its description before downloading values, then
keep the result in a collection that can travel to another computer.

The common workflow is available for **Nomis, StatsWales and MHCLG**. Their
selection codes and supported formats differ; check the
[coverage matrix](../reference/sources.md) before choosing a source.

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

Create a connector using `"nomis"`, `"statswales"` or `"mhclg"`:

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
complete teaching script below supplies concrete examples. Nomis and StatsWales
can return statistical observations with dimensions and measures. An ordinary
MHCLG CSV is retained as records, preserving its column names and source text.
SSDU does not guess units or make different tables comparable.

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

## Read the complete script

The self-contained script is available as a
{download}`download <../_downloads/provider_workflow.py>`.

```{literalinclude} ../_downloads/provider_workflow.py
:language: python
```
