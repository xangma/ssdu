# Provenance and interpretation

SSDU records the bytes and metadata behind retained data. A snapshot contains
source bytes, a normalised Parquet table, metadata and a hashed manifest. The
manifest identifies the request, provider, dataset, release information,
attribution, capture time and file hashes.

For local imports, the source identity is a SHA-256 URN for the exact input bytes.
You supply the dataset description, column meanings, types, citation and rights.
CSV columns default to text unless explicit types are given; Parquet retains
supported native types. Importing a file does not verify the truth of its metadata.

## What a collection lock pins

A lock binds each entry's snapshot, metadata version, query and expected result
hash. Replay verifies the retained snapshot and pinned interpretation, runs the
query, and rejects a changed result. Export/import preserves those identities.

Dataset identity, snapshot identity and collection identity answer different
questions. A dataset can have several releases or captures; a collection can
select a particular capture and query. Capture time is separate from provider
release date and the period described by the observations.

Use the [Python reference](../reference/python.md) for verification and replay,
or the [REST](../reference/rest.md) and [MCP](../reference/mcp.md) provenance reads.
`Store.read_intake()` verifies a snapshot before reading through Intake. Reading
an Intake catalogue directly does not add SSDU's verification step.

## Check analytical fit

Before combining or interpreting data, check:

- The original source, release and exact selection, including suppressed or
  missing observations.
- Measure definitions, units, population and reference periods.
- Geography codes and vintages; boundary changes can prevent direct joins.
- Access conditions, attribution and licence for both source data and transfer.
- What is known, what is explicitly incompatible, and what remains unknown.

For example, English deprivation releases can use different LSOA and local
authority vintages. An index release year is not a single observation year for
all its indicators. Welsh country-level poverty data do not establish Portsmouth
coverage. Claimant count, unemployment, income poverty and area deprivation are
different measures.

A missing coverage field means **unknown**. A relevant catalogue record or a
publication's use of a dataset does not prove suitability for a new question.
An empty result in a retained catalogue does not prove that no source exists.

## Software licence and source terms

SSDU's software and documentation use the
{download}`MIT licence <../_downloads/LICENSE.txt>` from the repository's `LICENSE` file.
Provider datasets, metadata and code lists have their own source terms; the
software licence does not replace them. Check each source's access, attribution
and redistribution conditions before acquiring or transferring its material.

Source publication excludes provider data and raw metadata, research corpora,
snapshots and collection bundles. Real acquisition and research evidence stays
in private local working files. Published tutorials use independently invented
examples, clearly labelled as synthetic; their rows are not official statistics.

## Integrity and scientific evidence

Hash verification establishes that retained bytes match their recorded identity.
It does not establish statistical validity, current provider availability,
historical publication vintage or permission to redistribute. Keep the source's
quality and comparability caveats with any result you cite.

Research relevance labels and suitability judgments have their own provenance
and versions. The latest overlay is agent-adjudicated, and human validation
remains pending. See [discovery research](../research/index.md).
