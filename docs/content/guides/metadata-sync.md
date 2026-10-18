# Keep a catalogue you can search offline

Before choosing a table, you may want to search the descriptions available from
several services. SSDU can capture their catalogues in a **local metadata cache**.
It saves the provider responses, gives their descriptions a common structure and
builds an index that a discovery application can read without contacting providers.

Metadata describes data: titles, definitions, categories, source links and stated
coverage or access conditions. Catalogue capture does not download observations
or attachments. It retains entries even when SSDU cannot retrieve their files.
Check the [provider coverage matrix](../reference/sources.md) for download support.

The same capture workflow covers **Nomis, StatsWales, MHCLG, NISRA and Scotland's
current `data.gov.scot` portal**. Their catalogue units differ: MHCLG entries are
publication pages, potentially containing several files; the other catalogues
mainly list datasets or tables. A full service catalogue is not every holding of
the organisation that maintains it.

## Start by searching a small catalogue

The [metadata discovery tutorial](../start/metadata-discovery.md) creates two
invented entries, searches their descriptions and explains the returned fields.
It shows both Python and command-line search, including a record that is useful
to discover even though SSDU cannot download its observations.

Use that lesson first if you want to understand search results. Continue below
when you want to capture catalogues for your own application.

## Capture and transfer a teaching catalogue

After [installation](../start/install.md), run from the project folder:

```sh
uv run --frozen --offline python examples/metadata_capture.py --output-dir data/metadata-capture
```

Choose a new or empty output folder. This example uses **independently invented
metadata** for all five services and blocks network connections. It plans a
capture, stops after a saved batch, resumes it and reads the completeness report.
It then exports the capture, moves the original store aside, imports into a fresh
store and replays the retained responses offline.

Look for `"status": "passed"`, `"provider_network_requests": 0` and
`"fresh_store_replayed_offline": true`. The output includes `result.json` and a
portable metadata bundle. The {download}`self-contained script
<../_downloads/metadata_capture.py>` also runs outside the checkout after installing
SSDU.

For a smaller, single-job introduction with category browsing, use
{download}`metadata_sync.py <../_downloads/metadata_sync.py>`.

## Plan a full service capture

A capture first saves an inventory, then describes its entries in bounded batches.
Planning is local and makes no HTTP requests:

```python
from ssdu.metadata_capture import MetadataCapture

cache = MetadataCapture("my-data")
spec = {
    "providers": {
        provider: {
            "detail": "dataset",
            "page_size": 100,
            "max_pages": 100,
            "max_datasets": 10000,
        }
        for provider in ("nomis", "statswales", "mhclg", "nisra", "scotland")
    }
}
plan = cache.plan_capture(spec, batch_size=10)
print(plan)
```

These are ceilings, not expected counts. A catalogue larger than the chosen
limits produces a partial report. Increase bounds deliberately after inspecting
the plan and service scope. Use `detail="codes"` only when category lists are
needed; it can require many more requests. Use the smaller `MetadataSync` workflow for catalogue-only summaries.

Submit the plan with a stable name for this capture:

```python
job = cache.submit_plan(plan["plan_id"], "research-catalogue-1")
result = cache.run_job(job["job_id"])
print(result)
```

Acquisition uses SSDU's persistent HTTP cache, shared per-host rate limits and
request/byte budgets. Provider backoff remains in force. If the process stops,
reopen `MetadataCapture` with the same store and run the same job ID again.
Checkpoints preserve completed batches. `get_job(job_id)` shows progress and
failures; an idempotency key prevents accidentally submitting the same job twice.

To give control back after one batch, use `run_job(job_id, max_batches=1)` and
continue the same job later. A completed capture has a `capture_id`; inspect its
immutable report with `cache.report(capture_id)`.

The command line offers the same explicit steps. Save `spec` as `capture.json`:

```sh
ssdu --store my-data metadata capture plan capture.json --batch-size 10
ssdu --store my-data metadata capture submit PLAN_ID --idempotency-key research-catalogue-1
ssdu --store my-data metadata capture run JOB_ID
ssdu --store my-data metadata capture report CAPTURE_ID
```

`uv --offline` only prevents package downloads. Use SSDU's `offline=True` or the
capture command's `--offline` option to require retained provider responses.

## Read a common description without losing the original

SSDU normalizes metadata into a versioned structure while retaining native
responses and references. It does not infer statistical meaning from titles.

| Common field | What it represents |
| --- | --- |
| `description` | Provider prose, with HTML removed. |
| `publishers`, `subjects`, `languages` | Declared organisations, topics and language information. |
| `dimensions`, `measures` | Declared categories and statistics, including units where stated. |
| `coverage` | Dates, areas or category roles, labelled with their source and scope. |
| `distributions` | Files or services described by the provider, including unsupported formats. |
| `licence`, `access` | Stated terms and access conditions; absence remains unknown. |
| `related` | Declared links to other records or publications. |
| `field_status` | Whether a field is mapped, absent from the response, unmapped, ambiguous or not requested. |

Field status separates a gap in SSDU's interpretation from a missing provider
statement. Mapping evidence records the native locations and retained response
hashes behind normalized values. The current contract is `1.1.0`, with mapping
`1.3.0`. Older snapshots remain readable; replaying their retained responses creates
a new interpretation without changing the original snapshot.

Read coverage with its `scope` and, where present, `basis`. A time dimension means
periods can be selected; it does not establish the first and last available
observations. MHCLG publication applicability is distinct from the geographic
extent of its tables. Normalizing fields does not make measures or geographies
comparable across providers.

`access.retrieval` answers a separate question: **can this SSDU adapter acquire
this record?** It reports `supported`, `status`, a reason and any constraints.
It does not grant permission to reuse the data. MHCLG publication pages can be
discoverable while containing no supported file; some NISRA cubes exceed SSDU's
whole-cube size limit. Read `licence` and the stated access conditions separately.

Provider dates keep their original spelling, precision and timezone information
under `source_metadata.provider_timestamps`. Publication and modification dates
have separate roles. A date with no timezone stays unspecified; SSDU does not
turn it into a UTC instant. These provider assertions are separate from the
time SSDU successfully checked a response.

Scotland's current CKAN catalogue is metadata-only in SSDU. Resource links and
formats are retained without claiming that SSDU can retrieve their observations.
Saved legacy `statistics.gov.scot` snapshots remain replayable. The legacy service
stopped receiving updates on 25 August 2026 and is scheduled to close on 31 October
2026. See the [official transition notice](https://blogs.gov.scot/digital/2026/08/19/data-gov-scot-public-beta-launches-on-25-august/).

## Understand completeness

Read the whole-capture report before describing a cache as complete. It separates:

- **Inventory coverage:** distinct entries found, declared totals, pagination and reconciliation results.
- **Description coverage:** inventoried entries successfully described, with unresolved IDs and errors.
- **Field quality:** counts of mapped, missing, unmapped and ambiguous fields.
- **Freshness:** capture time, successful provider-check time and any provider modification date.

An exhausted, reconciled inventory supports a claim about that declared service
scope. It does not establish every release, historical archive, independent
codelist, restricted dataset or other service operated by the publisher. Where
there is no verified total, a percentage is unknown. A provider can also change
while its pages are read; the capture is not an atomic upstream snapshot.

Limits and failures remain visible. Missing entries in a partial capture do not
imply deletion. A cache hit does not mean the provider was checked today. Use
`refresh=True` when deliberately rechecking; capture does not create a recurring
refresh schedule.

## Transfer and replay the cache

A capture consists of verified immutable snapshots and a report relating them.
Export it when another machine needs the same discovery material:

```python
cache.export_capture(capture_id, "research-catalogue.ssdu-metadata.zip")

fresh = MetadataCapture("fresh-data")
imported = fresh.import_capture("research-catalogue.ssdu-metadata.zip")
print(imported)
```

Import verifies the bundle before using it. The saved snapshots preserve references
and original check times. A new `plan_capture(spec, offline=True)` can replay the
retained responses in the fresh store. Search results come from a rebuildable
local index; searches do not trigger acquisition. REST and MCP expose the retained
metadata and capture report, while Python, CLI and workers perform explicit capture.

## Understand a search result

The index searches readable descriptions, topics and category labels. It excludes
internal evidence keys and raw provider documents from search terms.

A recent catalogue check can confirm that an entry still exists without checking
its full description again. SSDU can use a retained description to enrich that
entry, while keeping its older check time visible in `metadata_projection`.
The `catalogue` and `descriptor` sections identify their source snapshots and
freshness separately; `field_sources` identifies which snapshot supplied each
field. A known revision conflict prevents enrichment. Matching identity alone is
labelled `identity_only` with an unverified revision.

`metadata_profile.descriptor_available` tells you whether a usable descriptor
was retained. `description_text_available` tells you whether prose is present.
The older `description_available` flag still describes the current source
snapshot. Request an explicit `snapshot_id` when you need that historical
description rather than the combined current view. Index rebuilds preserve
snapshots and their original clocks.
