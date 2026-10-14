# Keep four national datasets in one collection

A research project may need data from England, Wales, Scotland and Northern
Ireland. SSDU can save supported selections from these sources in one
**collection**, while keeping each dataset's original description and meaning.

This example uses **invented teaching responses, not official statistics**.
It shows how to save work, recover an interrupted download and transfer the
collection for use offline. It does not produce a comparable indicator for the
four nations. If this is your first example, start with the
[guided tutorial](../start/research-tutorial.md).

## What goes into the collection?

| Area | Teaching example and support limit |
|---|---|
| England | An invented table in the English deprivation File 7 format, with its release and geographical boundaries recorded. |
| Wales | Invented StatsWales data, with the source's categories, units and notes. |
| Scotland | An invented linked-data table. File/ZIP downloads and components attached to slices are outside current support. |
| Northern Ireland | An invented NISRA portal JSON-stat table. The flexible census builder is outside current support. |

Each dataset has its own identifier and selection codes. Matching column names
alone do not mean that figures measure the same thing. Check the
[source coverage matrix](../reference/sources.md) for supported formats before
choosing real data.

## Run the example

[Install SSDU](../start/install.md), then run from the `ssdu` project folder:

```sh
uv run --frozen --offline python examples/four_nations.py --output-dir data/four-nation-example
```

Choose a folder that does not exist; the script will not overwrite an existing
run. It uses the helper and teaching files included with the project.

The script:

1. Describes four selections and plans the work needed to save them.
2. Starts a retrieval job and deliberately interrupts it part-way through.
3. Resumes the job, reusing work already saved.
4. Saves the finished collection and exports it to a bundle.
5. Opens the bundle in a fresh store and checks the same results offline.

Look for `verified` and `fresh_store_verified` set to `true`, and `job_status`
set to `succeeded`. The interruption and recovery happen within the script;
you do not need to stop it or rerun it yourself.

The {download}`collection description <../_downloads/four-nations.yaml>` shows
the four selections. The Scottish and NISRA identifiers are invented for
teaching and must not be used as live requests. You can also download the
{download}`complete Python script <../_downloads/four_nations.py>`.

## What happens after an interruption?

SSDU saves completed work and checkpoints as a job runs. A resumed job can reuse
that verified work, subject to its remaining request, byte and retry limits.
Any unfinished download may still need an internet connection. A cached response
does not check whether the provider has since issued new data; refreshing is a
separate choice.

Offline reads require the needed files to be present. They cannot finish a
missing download. The `uv --offline` option controls package downloads
separately; read [working offline](../concepts/offline.md) for the distinction.

## Keep the meanings with the figures

An exported bundle carries the saved data and their descriptions. A collection
lock alone only records which saved data and selections to use. The receiving
computer also needs SSDU and its dependencies installed.

Before comparing national figures, check populations, definitions, units,
observation periods, release dates and geographical boundaries. A deprivation
rank and a poverty rate remain different measures even when both have a numeric
`value` column. Missing information remains unknown.

Use the saved [source information](../concepts/provenance.md) when citing results.
The example demonstrates a workflow with invented inputs; it does not establish
that every source table is currently available or suitable for an analysis.
