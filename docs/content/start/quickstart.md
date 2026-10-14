# Offline quickstart

Create a synthetic CSV, retain it as a snapshot, lock a query, and replay it in a
fresh store after transfer. This demonstrates a complete local workflow without
provider data or a prepared cache.

## Run

[Install the locked environment](install.md), then run from the repository root:

```sh
uv run --frozen --offline python examples/documentation_quickstart.py --output data/quickstart
```

Choose an absent or empty output directory. The example refuses to overwrite an
existing run. The rows are invented tutorial data and must not be used as official
statistics.

The script performs these steps with assertions:

1. Write a small CSV and explicit metadata, including column types and rights.
2. Import the file into a new store, retaining source bytes and a hashed manifest.
3. Lock a bounded query with its snapshot, metadata and expected result identity.
4. Export the collection and remove the original CSV.
5. Import into a second new store and replay the transferred collection.
6. Compare the data, metadata and identities before reporting success.

The JSON result contains `"status": "passed"`, the selected row
`{"code": "002", "value": 7}`, snapshot and lock identifiers, and
`"original_input_removed": true`. The `notes` collection contains one
`observations` entry whose query selects values of at least five.

The transferred collection contains the retained inputs it needs; replay does
not reopen the original CSV or request provider data. The script blocks socket
connections and DNS lookup before invoking SSDU. Each new run creates its own
capture, so identities from separate runs need not match.

## Read the example

The complete script below is also available as a
{download}`download <../_downloads/quickstart.py>`. It uses the public modules
listed in the [Python reference](../reference/python.md).

```{literalinclude} ../_downloads/quickstart.py
:language: python
```

## What this establishes

A collection lock pins data and interpretation for a specific query. It preserves
replay across transfer; it does not establish that your own data are complete,
comparable or correctly interpreted. Inspect the retained
[provenance](../concepts/provenance.md), and supply accurate metadata and rights
when importing your own CSV or Parquet file.

The example uses local-only operations. `uv --offline` also prevents dependency
downloads, but does not itself block Python networking. See
[offline operation](../concepts/offline.md) for the distinction.
