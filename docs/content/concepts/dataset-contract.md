# Dataset contract

`Dataset` gives retained tables a machine-readable description of their structure
and meaning. Supported provider data uses the `observations` profile in contract
version `1.0.0`. Local CSV/Parquet imports use the `records` profile in version
`1.1.0`; their typed records do not have to become numeric observations.
Contract versions are separate from the package version and immutable snapshot
identifier. Existing observation contracts remain compatible.

Native requests still use each provider's selectors. The
[source coverage reference](../reference/sources.md) states which sources and
transformations are implemented and the limits of their validation.

## Read observations and metadata

`Store.fetch_dataset(request)` acquires or reuses a selection and returns a
`Dataset`. Use `offline=True` to require retained inputs. Pin its `snapshot_id`
and use `Store.open_dataset(snapshot_id)` to verify and reopen it locally without
acquisition or mutation.

For a provider `observations` dataset returned by a
[provider example](../guides/dataset-examples.md):

```python
observations = dataset.to_arrow()     # defaults to "observations"
frame = dataset.to_pandas()           # the same observation table as pandas
source = dataset.to_arrow("source")   # retained provider table
metadata = dataset.contract          # JSON-compatible dictionary
descriptor = dataset.descriptor      # typed DatasetDescriptor
print(descriptor.quality.completeness)
```

The descriptor records source identity, effective request, release, attribution,
raw-input hashes, parent snapshots and transformation version. It also describes
tables, dimensions, variables, coverage and quality diagnostics. See the generated
[Python](../reference/python.md) and [schema](../reference/schemas.md) references
for signatures and field constraints.

## Preserve meanings

Observation dimensions have string codes and a declared kind: geography, time,
category or identifier. Geography level and vintage are optional; missing
information remains unknown. Code labels explain values without replacing their
identities or dropping leading zeroes.

| Observation field | Meaning |
|---|---|
| `measure` | Identifier linked to a variable definition. |
| `value` | Finite numeric value, or null when the source token cannot be interpreted numerically. |
| `value_raw` | Source spelling retained separately from numeric interpretation. |
| `unit` | Unit when supported by source evidence; missing units stay unknown. |
| `status` | Source or transformation status; inspect its meaning before using a null. |
| `notes` | Retained qualifications or notes. |

Zero, missing and suppressed values remain distinct. For example, numeric `1`
can retain source token `001`; a suppression token can remain in `value_raw`
while `value` is null. Ranks, deciles, scores and counts are distinct measures.
Reshaping several measures from one source record can increase the observation
row count.

The `source` table retains the provider table alongside observations. Original
response bytes remain bound through snapshot provenance. Common columns do not
establish comparable measures across sources or geography vintages.

## Preserve local record structure

The `records` profile describes CSV/Parquet fields with their types, meanings,
units and null semantics. Use `Store.import_dataset(path, LocalImportSpec(...))`
to import a file and return a `Dataset`; the legacy `import_local()` function
continues to return a manifest. `to_arrow()` and `to_pandas()` default to
`records` for this profile.

Declare `primary_key` and `row_grain` explicitly: identify what one record
represents and which fields identify it. Declared keys are checked for unique,
non-null values. A key does not establish that a field is a statistical measure
or that records from different datasets can be compared. Column declarations
can supply `label`, `definition`, `unit` and `missing_values`; absent explanations
remain unknown.

Preserve identifiers, text and typed values rather than forcing every field into
`measure` and `value`. Missing meanings, units or null explanations remain unknown;
do not infer them from a column name or a numeric type. Consult the descriptor
before choosing the `records`, `observations` or retained `source` table.

## Inspect and query a retained snapshot

The [common dataset interface](../guides/dataset-api.md) describes a snapshot,
queries a selected contract table and lists dimension values across Python, CLI,
REST and MCP. These reads use the retained snapshot rather than acquiring a new
provider selection.

Metadata inspection is bounded. Dimension summaries report `code_count` and
`codes_omitted` instead of embedding code arrays. Use the paginated dimension
operation to inspect observed codes. Those codes describe retained rows; they
are not an exhaustive catalogue of everything a provider could return.

## Interpret completeness

Quality records source and observation row counts, with `records_rows` for the
records profile, diagnostics, and completeness as `complete`, `partial` or
`unknown`. Completeness concerns the requested
selection, not every dataset or observation held by the provider. Observed
coverage describes retained evidence; it does not prove suitability for a new
population, period or geography. Review [provenance](provenance.md) before analysis.

## Open historical snapshots

`open_dataset()` rejects a historical snapshot without a dataset contract.
Call `Store.migrate_dataset(snapshot_id)` to create a new derived snapshot for a
supported provider or historical local import. A local migration reparses the
verified original CSV/Parquet bytes using the retained import policy; it requires
both. Migration never contacts a provider. The original bytes, request pointers
and collection locks remain intact. `fetch_dataset()` performs this local
migration when a cached provider selection needs it. Dataset API reads require
an existing contract and never migrate it implicitly.

Existing `Store.fetch()` and `Store.replay()` retain their manifest and original
table behaviour. The legacy `query_data()`/`QuerySpec` interface and historical
collection-lock serialization retain their existing contracts. A local records
profile does not convert arbitrary imports into the provider observation profile.
