# Keep variables, questions and response codes

Download {download}`metadata_graph.py <../_downloads/metadata_graph.py>` after
[installing SSDU](../start/install.md), then run it in the download folder:

```sh
python metadata_graph.py --output-dir metadata-example
```

Choose a new or empty folder. This self-contained example uses **invented metadata**
and blocks network connections. It captures two study descriptions, their tables,
variables, explicit questions and response codes. It stops and resumes a job,
reuses cached responses, then transfers the result to a fresh store and replays it
in a separate Python process with HTTP, DNS and sockets blocked.

Look for `"status": "passed"`, `"network_attempts": 0` and
`"offline_replay_equal": true`. The folder contains `result.json`, a separate
`capture-report.json` and a verified metadata bundle. A passed example means its
checks succeeded; consult the report for the scope and completeness of a capture.
No observation records are downloaded.

## Read the teaching catalogue

Each **entity** describes one thing, such as a variable or question. A **link**
records a relationship, such as a variable having a code list. Read the transferred
snapshot using the identifier saved by the example:

```python
import json
from pathlib import Path
from ssdu import MetadataGraphStore

result = json.loads(Path("metadata-example/result.json").read_text())
snapshot_id = result["offline_snapshot_id"]
graph = MetadataGraphStore("metadata-example/fresh")
graph.verify(snapshot_id)

for kind in ("variable", "question", "code_list", "code"):
    page = graph.entities(snapshot_id, kind=kind, limit=20)
    print(kind, page["total"])
    for entity in page["items"]:
        print(entity["origin"], entity["label"], entity["attributes"])
```

You should find two variables, two questions, two code lists and four codes. The
same invented native identifiers appear under two origins; their SSDU identifiers
remain distinct. The response code `"01"` retains its leading zero. Code `"99"`
means **Not stated**, with `isMissing: true`; it is not another substantive answer.
Question text comes from an explicit question field, rather than being inferred
from a variable description.

`graph.links(snapshot_id, limit=20)` returns source and target entity identifiers
and a relationship name. Resolve an identifier with
`graph.get_entity(snapshot_id, entity_id)`. Both list methods return `next_offset`;
continue at that offset until it is `None`. Page limits are 1–100.

Every entity and link carries references to retained responses, including their
hashes, JSON locations and original capture/check timestamps. Reading or importing
a snapshot does not recheck the provider or make those timestamps newer.

## Choose a metadata service

Metadata-only connectors describe catalogues independently of SSDU's data-download
adapters. Listing them is entirely local:

```python
from ssdu import list_metadata_connectors

for connector in list_metadata_connectors():
    print(connector.id, connector.catalogue_url, connector.scope)
```

The reviewed MetadataWorks registrations are `adr_uk`, `nisra_secure` and
`research_data_scotland`. They describe separate public catalogue scopes. Source
origins remain separate within a federated catalogue, and geographic coverage does
not establish membership of an ADR partnership. A registration does not make the
underlying restricted data downloadable. Dictionaries provided only as linked
attachments are not captured by this connector.

The earlier [metadata cache](metadata-sync.md) uses the dataset-summary contract
`ssdu.metadata` version **1.1.0**, with `MetadataIndex` for local text search. Deep
capture uses the separate `ssdu.metadata.graph` contract **1.0.0** and
`MetadataGraphStore` for entity and relationship reads. Deep captures do not
automatically populate `MetadataIndex`; read them through the graph interfaces
shown here.

## Plan a bounded service capture

The following example prepares a real ADR UK catalogue capture. **Planning and
submission make no HTTP requests; running the job contacts the service.** Change
the connector identifier to use either of the other reviewed tenants.

```python
from ssdu import MetadataGraphCapture

capture = MetadataGraphCapture("my-metadata")
spec = {
    "connector": "adr_uk",
    "page_size": 25,
    "max_pages": 20,
    "max_tasks": 200,
    "max_entities": 20000,
}
plan = capture.plan_capture(
    spec,
    budget={"max_requests": 100, "max_total_bytes": 20000000},
)
job = capture.submit_plan(plan["plan_id"], "adr-metadata-1")
print(plan["warnings"], job["job_id"])
```

Save the job identifier. The idempotency key `adr-metadata-1` identifies this
submission; repeating it returns the same job. A different plan needs a different
key. The limits are ceilings, not estimates of catalogue size or cost. `page_size`
controls the dataset inventory; table and dictionary requests use separate
100-record batches, still subject to request, task and byte budgets.

Run at most five operations, then inspect progress:

```python
progress = capture.run_job(job["job_id"], max_tasks=5)
print(progress["status"], progress["tasks"], progress["retry_at"])
```

To continue after restarting Python, reopen `MetadataGraphCapture("my-metadata")`
and call `run_job(saved_job_id, max_tasks=5)` with the same identifier. Completed
operations and budgets survive a restart. `max_tasks` on `run_job` yields control;
it does not increase the plan's hard limits.

Requests use SSDU's persistent cache and shared per-host rate limits. The default
pacing policy allows one request per second and one in-flight request per host on
the same machine. Provider cooldowns remain in force. A `waiting` job reports an
absolute `retry_at`; resume after that time. Repeated calls before it do not bypass
the wait. Use `refresh=True` when planning a deliberate recheck, or `offline=True`
to require retained responses. These options cannot be combined.

When `progress["result"]` is present, its `snapshot_id` identifies the immutable
graph. Open it with `MetadataGraphStore("my-metadata")` and read it as above.
`capture.get_job(saved_job_id)` inspects the job without running it.

The equivalent command-line workflow uses the same `spec` saved as `graph.json`:

```sh
ssdu --store my-metadata metadata graph connectors
ssdu --store my-metadata metadata graph plan graph.json
ssdu --store my-metadata metadata graph submit PLAN_ID --idempotency-key adr-metadata-1
ssdu --store my-metadata metadata graph run JOB_ID --max-tasks 5
ssdu --store my-metadata metadata graph job JOB_ID
ssdu --store my-metadata metadata graph report SNAPSHOT_ID
ssdu --store my-metadata metadata graph entities SNAPSHOT_ID --kind variable --limit 20
```

Replace the capitalized identifiers with returned values. `plan --budget budget.json`
accepts the same budget object as Python; `plan --offline` requires cached responses.

## Read completeness before using the cache

Use `graph.report(snapshot_id)` or `capture.report(snapshot_id)`. The report
separates captured counts, declared totals, exhausted traversal, unknown coverage,
failures, unresolved references and bounds for each entity kind.

`traversal_complete` concerns the supported traversal. `complete` also considers
unknown coverage; finishing a queue does not establish that every variable or
question offered by a service was published. A declared total is only a denominator
for its stated public endpoint scope. Hidden holdings, separate catalogues and
attachment dictionaries remain outside that scope.

Current MetadataWorks object versions can be unknown. SSDU retains published
version-history entries as release descriptions; it does not invent historical
dictionary endpoints or treat those descriptions as captured historical variables.
Question and code-list inventories may have no published denominator.

Bounds, inconsistent totals and repeated pages produce visible gaps or partial
results. They do not imply that uncaptured entries were deleted. A capture also
spans multiple upstream requests, so it is not an atomic provider snapshot. Review
the report before making any claim about coverage of a service or its providers.

## Move the cache and replay it offline

Export the chosen snapshot and import it into another store:

```python
graph.export(snapshot_id, "research-metadata.ssdu-metadata.zip")

fresh = MetadataGraphStore("fresh-metadata")
imported_id = fresh.import_bundle("research-metadata.ssdu-metadata.zip")
fresh.verify(imported_id)
print(fresh.entities(imported_id, kind="question", limit=20))
```

The bundle contains normalized entities, relationships, the report and retained
metadata evidence. Import verifies them before publication in the destination.
For a new offline capture, use `MetadataGraphCapture("fresh-metadata")` and
`plan_capture(spec, offline=True)`; the worker seeds its response cache from
verified snapshots. It never falls back to a live request if evidence is missing.

CLI transfer uses `metadata graph export SNAPSHOT_ID --output FILE` and
`metadata graph import FILE` with the appropriate `--store` value.

Applications can read the same retained graph through REST
`/metadata/graphs/{snapshot_id}/entities`, `/links` and `/report`, or the MCP tools
`list_metadata_entities`, `list_metadata_links` and `metadata_graph_report`.
These reads do not acquire or refresh metadata. See the [Python](../reference/python.md),
[REST](../reference/rest.md) and [MCP](../reference/mcp.md) references for exact inputs.
Keep provider material local and check its stated reuse terms before sharing it.
