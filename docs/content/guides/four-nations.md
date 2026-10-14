# Four nations, distinct datasets

A four-nation collection can preserve separate source-native datasets for
England, Wales, Scotland and Northern Ireland. A common
[dataset contract](../concepts/dataset-contract.md) makes their tables and
metadata accessible through the same API while retaining their original meanings.

The teaching workflow uses **synthetic responses, not published statistics**.
Its purpose is to demonstrate retained acquisition, durable jobs, verified
transfer and offline replay. Check the generated
[source coverage reference](../reference/sources.md) for current implemented
support and validation limits before choosing real data.

## Keep the four sources distinct

| Teaching case | Source boundary |
|---|---|
| England | Invented observations in the English deprivation File 7 shape, retaining release and geography vintage. |
| Wales | A native StatsWales dataset, retaining its dimensions, units and notes. |
| Scotland | An invented linked-data cube; file/ZIP routes and slice-attached components are unsupported. |
| Northern Ireland | A NISRA portal JSON-stat cube; the flexible census builder is outside this scope. |

Each case needs its own dataset identifier and native selectors. Preserve those
requests, source references and release evidence rather than assuming matching
column names identify equivalent statistics. The Scottish and NISRA teaching
scopes do not establish conformance across their entire portals.

## Run the teaching workflow

[Install SSDU](../start/install.md), then run from the repository root:

```sh
uv run --frozen --offline --extra dev python examples/four_nations.py --output-dir data/four-nation-example
```

Choose a directory that does not exist. The example refuses to overwrite an
existing path. Keep its helper, fixture manifests and collection specification
with the checkout; source-only installation needs no retained provider capture.

The {download}`collection specification <../_downloads/four-nations.yaml>` uses
provider IDs `deprivation`, `statswales`, `scotland` and `nisra`. Its dataset IDs
and selectors demonstrate four distinct native interfaces. The Scottish series
and NISRA table identifiers are invented teaching identifiers, not live requests.
Inspect real provider metadata before choosing a published selection.

The {download}`complete script <../_downloads/four_nations.py>` runs this sequence:

1. Prepare four explicit requests and a collection specification.
2. Plan and queue a durable retrieval job with bounded acquisition budgets.
3. Exercise an interruption and recovery, checking reuse of retained work.
4. Inspect each dataset's observations, source table, metadata and diagnostics.
5. Lock and export the collection, then import its bundle into a fresh store.
6. Reopen the datasets and replay the lock with networking blocked, comparing
   contracts, tables and pinned results across transfer.

The deliberate interruption occurs after two retained fixture responses. England's
entry has completed; a new worker resumes persisted job state and reuses the two
response checkpoints. Recovery is part of this script, so rerunning the command
against the same output directory is not the recovery mechanism.

Success reports `verified` and `fresh_store_verified` as `true`, `job_status` as
`succeeded`, and `worker_restarted` as `true`. `retained_responses_reused` is `2`
and `completed_entries_reused` contains `england`. All cached request counts are
zero. A separate destination process checks source bytes, normalized tables,
Intake reads and collection-query results, with `network_attempts` and
`source_store_attempts` both zero. `job_requests` counts in-memory fixture HTTP
attempts, not provider network traffic. Live request-pacing settings remain intact.

## Cache and resume deliberately

Verified cache hits reuse the retained response selection. They do not check the
provider for a newer release. Durable jobs retain completed entries and request
checkpoints so recovery can reuse verified work; unfinished acquisition may still
need provider requests. Retry remains subject to the job's remaining request,
byte and attempt budgets.

An application offline policy requires retained inputs. It cannot complete a
missing selection by downloading it. `uv --offline` separately governs dependency
downloads and does not block Python networking. See
[offline operation](../concepts/offline.md) for these boundaries.

## Verify transfer and interpretation

A collection bundle carries retained snapshots and pinned metadata; a lock file
alone does not carry all replay inputs. Verify identities and query results after
import into a fresh store. Keep the installed code and dependencies available:
the bundle is data and evidence, not a Python runtime.

Contract version `1.0.0` provides an observation profile; it does not harmonise
methodology. Check population, measure definitions, units, observation periods,
release dates and geography vintages independently. Missing coverage or units
remain unknown. In particular, a deprivation score or rank must not be treated
as a directly comparable measure from another nation's dataset merely because
both expose numeric values.

Use the retained [provenance](../concepts/provenance.md) and source caveats when
citing or combining results. Synthetic fixture success establishes the exercised
software path, not current live availability, all-portal conformance or analytical
suitability.
