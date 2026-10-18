# Find, select and save a provider dataset

Suppose you find a useful table today and want a colleague to reopen the same
figures next month. SSDU's `Connector` takes you from a provider's catalogue to a
saved dataset. You can inspect its description before downloading values, then
keep the result in a collection that can travel to another computer.

The common workflow is available for **Nomis, StatsWales, MHCLG, NISRA and
Scotland**. Their selection codes and supported formats differ; check the
[coverage matrix](../reference/sources.md) before choosing a source.

**Scotland:** catalogue browsing uses the current `data.gov.scot` service.
Those entries are metadata-only: resource links do not yet imply download support.
Explicit legacy IDs still support the retained linked-data workflow described
below. The legacy service stopped receiving updates on 25 August 2026 and is
scheduled to close on 31 October 2026. Saved snapshots can still be replayed
offline. See the [official transition notice](https://blogs.gov.scot/digital/2026/08/19/data-gov-scot-public-beta-launches-on-25-august/).

To collect descriptions for discovery before choosing a table, see
[keeping a searchable metadata catalogue](metadata-sync.md).

## Start with a guided selection

The [provider selection tutorial](../start/provider-selection.md) follows one
invented Nomis table. You will see actual catalogue results, choose a dataset ID,
browse area and year codes, review a plan and read the selected rows. It runs
without contacting a provider and includes an offline reopening step.

## Use the workflow with real data

After practising, create a normal connector with one of the provider IDs above:

```python
from ssdu import Connector

provider = Connector("nomis", root="my-data")
page = provider.list_datasets("employment", limit=20)
print(page)
```

This normal client can contact the provider when metadata is not already cached.
Requests use SSDU's persistent cache and configured provider rate limits. Review
catalogue pages with `limit` and `offset`, choose an ID returned by that provider,
and call `describe_dataset` before constructing a request.

For an API table, `list_dimension_values` returns codes for categories such as
areas and years. Keep those codes rather than substituting display labels. The
[tutorial](../start/provider-selection.md) demonstrates the complete sequence with
concrete teaching codes; replace them with codes returned for your chosen table.
Nomis, StatsWales, NISRA and supported legacy Scottish cubes preserve different
selector syntax even though the connector methods are the same.

For MHCLG publications, choose a supported attachment from the description.
File-column selections happen locally after downloading that attachment. General
MHCLG CSVs use the records profile; statistical cubes use observations. Inspect
the descriptor before choosing a table. SSDU does not infer missing units or
make measures comparable.

Call `plan_fetch` to inspect the requested work before `fetch_dataset` saves the
values. Planning can require metadata requests; it does not download the
observation selection. After a successful acquisition, `fetch_dataset` with
`offline=True` requires the retained inputs. Keep the snapshot ID to reopen the
exact saved version.

## Save and transfer two publication tables

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

## Try Northern Ireland and Scotland

This second example follows the retained legacy workflow for two invented tables:
workshop attendance in Northern Ireland and community events in Scotland. The measures
are unrelated; putting them in one collection does not make them comparable.

```sh
uv run --frozen --offline python examples/national_connectors.py --output-dir data/national-connectors
```

It browses the NISRA catalogue and explicitly browses Scotland’s legacy catalogue,
reads the dataset description and available years, selects one year, inspects the
plan and saves a dataset. It then checks that
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
make an oversized cube downloadable. Legacy Scotland supports linked-data
observation cubes using bounded, selected queries; file/ZIP publications and slice-attached
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
