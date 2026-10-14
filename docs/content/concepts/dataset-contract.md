# Dataset contract

`Dataset` gives supported provider data a common observation table and a
machine-readable description. Native requests still use each provider's
selectors. The [source coverage reference](../reference/sources.md) states which
sources and transformations are implemented and the limits of their validation.

The current contract is version `1.0.0`, with profile `observations`. This version
describes dataset structure and meaning; it is separate from the package version
and the immutable snapshot identifier.

## Read observations and metadata

`Store.fetch_dataset(request)` acquires or reuses a selection and returns a
`Dataset`. Use `offline=True` to require retained inputs. Pin its `snapshot_id`
and use `Store.open_dataset(snapshot_id)` to verify and reopen it locally without
acquisition or mutation.

Once a [provider example](../guides/dataset-examples.md) has returned `dataset`:

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

## Interpret completeness

Quality records source and observation row counts, diagnostics, and completeness
as `complete`, `partial` or `unknown`. Completeness concerns the requested
selection, not every dataset or observation held by the provider. Observed
coverage describes retained evidence; it does not prove suitability for a new
population, period or geography. Review [provenance](provenance.md) before analysis.

## Open historical snapshots

`open_dataset()` rejects a historical snapshot without a dataset contract.
For a supported provider snapshot, call `migrate_dataset(snapshot_id)` to create
a new derived snapshot using retained evidence. It never contacts the provider;
the original bytes, request pointers and collection locks remain intact.
`fetch_dataset()` performs this local migration when a cached selection needs it.

Existing `Store.fetch()` and `Store.replay()` retain their manifest and original
table behaviour. Local CSV/Parquet imports still support snapshot and collection
replay, but conversion to this observation profile is not implemented.
