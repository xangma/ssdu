# Transfer a saved collection offline

This example shows how to move the data and selection behind an analysis to a
new folder, then read the same result without the original file or an internet
connection. It is a complete script rather than a step-by-step Python lesson.
New to SSDU? Start with the [guided tutorial](research-tutorial.md).

A **store** is a folder managed by SSDU. A **snapshot** is a saved copy of a
dataset, with its description and source information. A **collection** groups
the snapshots and selections you want to keep together.

## Run the example

[Install SSDU](install.md), then run from the `ssdu` project folder:

```sh
uv run --frozen --offline python examples/documentation_quickstart.py --output data/quickstart
```

Choose a new or empty output folder. The example will not overwrite an existing
run. All rows are invented teaching data, not official statistics.

The script:

1. Creates a small CSV and a description of its columns and source.
2. Imports it into a store and saves a selection of rows.
3. Packages the collection, then removes the original CSV.
4. Unpacks the collection into a fresh store and reads the saved selection.
5. Checks that the result still matches.

Look for `"status": "passed"` in the printed result. The selected row is
`{"code": "002", "value": 7}`. You will also see identifiers for the saved data
and `"original_input_removed": true`.

This shows why a saved collection is useful: someone receiving it can reopen
the same data without access to the original file. A collection lock records
what to use; the exported bundle also carries the files needed to use it.

## Read the Python behind it

The complete script is shown below and available as a
{download}`download <../_downloads/quickstart.py>`. Consult the
[Python reference](../reference/python.md) for individual functions.

```{literalinclude} ../_downloads/quickstart.py
:language: python
```

## Use this with your own work

Supply accurate descriptions, column types and source terms when importing your
own CSV or Parquet file. A successful transfer checks that the saved data and
selection match; it does not establish that the data are complete or suitable
for your research question. Read about
[sources and interpretation](../concepts/provenance.md) before citing results.

The script blocks network connections while it runs. The `uv --offline` option
separately prevents package downloads; it does not block Python networking by
itself. See [working offline](../concepts/offline.md) when adapting the workflow.
