# Where the data came from

Before using a figure, you need to know its source, what it measures and which
version you have. SSDU saves this source information, also called **provenance**,
alongside the data. A **snapshot** is that saved data and description together.

This helps you explain an analysis and return to the same inputs later. It does
not replace reading the source's definitions and quality notes.

## Check whether the data fit your question

Before combining or interpreting results, check:

- **Source and selection:** which dataset, release and rows did you use?
- **Meaning:** what is measured, for which population and in what units?
- **Dates:** which period do the observations describe?
- **Places:** which area codes and boundaries apply? Boundary changes can make
  apparently matching areas difficult to compare.
- **Missing values:** is a blank unknown, unavailable or deliberately withheld?
- **Terms:** what attribution, access and sharing conditions apply?

For example, an English deprivation release can use different area boundaries
from another release. Its publication year is not necessarily the year described
by every indicator. Country-level poverty data cannot answer a local-area
question unless suitable local observations are also available. Claimant count,
unemployment, income poverty and area deprivation are different measures.

Missing information remains **unknown**. A promising catalogue title or a paper
that used a dataset does not establish that it suits your question. Likewise,
an empty search in a saved catalogue does not prove that no source exists.

## Distinguish three dates

A dataset may describe observations from one period, be released by its provider
later and be downloaded by you later still. Keep the **observation period**,
**release date** and **download time** distinct. SSDU's saved-copy identifier
lets you refer to the particular copy used in your work.

For your own CSV or Parquet imports, you provide the title, column meanings,
units, citation and rights. SSDU checks the declared structure, but cannot verify
that your description is true. CSV columns default to text unless you declare
types; Parquet retains supported native types.

## Keep the data behind an analysis

A collection can save the snapshots and selections used together. Its **lock**
records exactly which saved copies, descriptions and query results to use.
An exported **bundle** also carries the files needed to reopen them elsewhere.

When SSDU replays a collection, it verifies the saved data and checks that the
result matches. This protects against unnoticed changes to the retained copy;
it does not establish that a measure is statistically valid or comparable with
another source. Keep the source's caveats with any result you cite.

## Cite and share responsibly

SSDU's software and documentation use the
{download}`MIT licence <../_downloads/LICENSE.txt>` from the repository's `LICENSE` file.
Provider datasets, metadata and code lists retain their own terms. Check those
terms before downloading or sharing a collection, and keep the required source
attribution with your work.

The published tutorials use invented examples, clearly labelled as synthetic.
Their rows are not official statistics.

## How verification works

For readers who need the technical detail: a snapshot retains source bytes, a
normalised Parquet table, metadata and a manifest listing file hashes. A hash is
an identifier calculated from file contents; SSDU uses it to detect changes.
For local imports, the source identity is based on the exact input bytes.

A collection lock binds the snapshot, metadata version, query and expected result
hash. Export and import preserve those identities. Verification establishes
that the retained bytes match their recorded identity, rather than proving
current provider availability, a historical publication date or permission to
redistribute.

Use the [Python reference](../reference/python.md),
[REST reference](../reference/rest.md) or [MCP reference](../reference/mcp.md) to
inspect source and verification details programmatically. `Store.read_intake()`
verifies a snapshot before reading through Intake; opening an Intake catalogue
directly does not add that SSDU verification step.
