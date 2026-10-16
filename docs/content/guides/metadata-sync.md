# Keep a catalogue you can search offline

Imagine preparing a research project before travelling. You want to search
dataset descriptions without repeatedly visiting five websites or downloading
the figures. SSDU can save provider catalogues and descriptions in a local
metadata snapshot, then build a search index from that snapshot.

**Metadata** describes a dataset: its title, categories, source links and any
declared dates, coverage or access conditions. Finding a catalogue entry does
not guarantee that SSDU can download every format it describes. Check the
[provider coverage matrix](../reference/sources.md) before retrieving values.

## Try it without contacting a provider

After [installation](../start/install.md), run from the project folder:

```sh
uv run --frozen --offline python examples/metadata_sync.py --output-dir data/metadata-sync
```

Choose a new or empty output folder. The example uses **independently invented
metadata** for Nomis, StatsWales, MHCLG, NISRA and Scotland. It supplies responses
in memory and blocks network connections. It contains no observations and does
not download attachments.

The example follows a complete research workflow:

1. Plan one metadata job covering all five providers.
2. Interrupt it partway through, then resume the same job.
3. Save a verified metadata snapshot and search its descriptions locally.
4. Inspect its freshness and completeness reports.
5. Export the snapshot, move the original store aside and import it into a
   fresh store.
6. Repeat the search and replay the retained provider responses offline.

Look for `"status": "passed"`, `"provider_network_requests": 0` and
`"fresh_store_replayed_offline": true`. The output includes `result.json`
and a metadata bundle. You can also run the
{download}`self-contained script <../_downloads/metadata_sync.py>` from another
folder after installing SSDU.

## Choose what to collect

A sync has three levels of detail:

| Detail | What you retain | When to choose it |
| --- | --- | --- |
| `catalogue` | Dataset identities and catalogue summaries | Start a broad search cheaply. |
| `dataset` | Summaries plus individual dataset descriptions | Inspect definitions and available dimensions. |
| `codes` | Descriptions plus bounded pages of category codes | Prepare selections such as years or areas. |

Large catalogues and code lists can take many requests. Set page and dataset
limits deliberately; a small exploratory sync can be useful even when it is
incomplete. Large code lists have their own limits, and unsupported code-list
operations remain visible in the report.

This Python example plans a bounded catalogue scan for all five providers:

```python
from ssdu.metadata_sync import MetadataSync

sync = MetadataSync("my-data")
spec = {
    "providers": {
        provider: {
            "detail": "catalogue",
            "page_size": 20,
            "max_pages": 2,
            "max_datasets": 40,
        }
        for provider in ("nomis", "statswales", "mhclg", "nisra", "scotland")
    }
}
plan = sync.plan_sync(spec)
print(plan)
```

Inspect the plan before submitting it. The limits bound how much metadata the
job attempts to collect; they do not promise an exhaustive provider catalogue.

```python
job = sync.submit_plan(plan["plan_id"], idempotency_key="research-catalogue-1")
result = sync.run_job(job["job_id"])
print(result)
```

The job uses SSDU's persistent cache and rate limits. If interrupted, reopen
`MetadataSync` with the same store and run the same job ID again. Retained
checkpoints let it continue without starting the entire scan over. Use
`get_job(job_id)` to inspect progress or a failure before retrying.

You can also save `spec` as JSON and use the command line:

```sh
uv run --frozen --offline ssdu --store my-data metadata plan sync.json
```

Here `uv --offline` prevents package downloads; the metadata job can still
contact providers. Use SSDU's explicit `offline=True` or the metadata command's
`--offline` option when retained responses are required.

## Understand the saved descriptions

SSDU keeps the provider's wording and identifiers, alongside a common set of
fields. The mappings use metadata already returned by each connector; they do
not download observations to fill gaps.

| Field | What it tells you |
| --- | --- |
| `description` | Readable provider prose, with HTML removed. |
| `measures` | Declared statistics or measure concepts, with units where stated. |
| `coverage` | Declared dates, areas, category lists or dimension roles, with their basis. |
| `licence` | The provider's stated licence; missing statements remain unknown. |
| `access` | The metadata endpoint and any stated access conditions. |

Read coverage with its `scope` and, where present, `basis`. A time dimension
means periods can be selected; it does not establish the first and last
available observations. NISRA category lists describe available metadata
categories. MHCLG publication applicability is labelled separately from an
observation extent. SSDU does not derive coverage from publication dates or
dataset titles.

Some fields will remain empty. For example, a Welsh filter labelled “Data
description” does not by itself identify which members are measures. A public
metadata endpoint also does not establish unrestricted data access or reuse:
`data_access` remains `not_asserted` without a provider statement.

Mapping version `1.1.0` retains each field's native location and description
response hashes in `source_metadata.mapping.evidence`. The metadata snapshot
contract remains `1.0.0`. Replaying older cached responses creates a new immutable
snapshot with the richer interpretation; it preserves provider-check times
and leaves the old snapshot unchanged. Large category lists remain complete in
Python and saved snapshots; REST/MCP responses mark compact summaries with
`metadata_detail` and `omitted_fields` and provide paged category browsing.

## Read freshness and completeness separately

A cache hit means SSDU already has a response. It does **not** mean the
provider was checked today. Reports distinguish when metadata was captured,
when a provider check last succeeded and any modification date declared by
the provider. Request `refresh=True` when planning a deliberate new check;
this still respects rate limits and budgets.

Completeness describes the requested scope and detail. A query for “housing”
can finish without covering a provider's other subjects. Reaching a page,
dataset or code-list limit leaves a partial result. Finishing catalogue pages
also does not guarantee that a provider stayed unchanged while they were read.
Review the report's scope, counts, limits and errors before describing a
catalogue as complete. Missing records in a partial run do not imply deletion.
The five connectors currently retain missing records after completed scans too:
their catalogue APIs do not provide the deletion evidence needed to retire them
reliably. A completed traversal is recorded as an observed inventory.

## Keep the catalogue used in an analysis

Each completed sync produces an immutable snapshot with the versioned metadata
contract and retained source responses. Export the snapshot when a colleague
needs the same discovery material; importing verifies the bundle before it is
used. Provider references remain attached to the records.

The current search index is a rebuildable view of verified metadata. Updating
it does not rewrite the descriptions pinned to previously downloaded data.
If a later catalogue-only refresh has less detail, the earlier description
remains available through its metadata snapshot ID with its original dates.
Keep the metadata snapshot ID with the research record when the exact search
corpus matters, alongside any [dataset collection](../start/quickstart.md).

The REST and MCP metadata interfaces read retained local material. Online
acquisition is an explicit Python, command-line or worker action. This makes
it possible to put a discovery application on top of the local index without
making each search trigger provider requests.
