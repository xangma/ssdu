# Provider dataset examples

Learn the [dataset contract](../concepts/dataset-contract.md) through three
executed notebooks. Each uses **synthetic teaching responses, not published
statistics**. The fixtures exercise SSDU's real adapters, request accounting,
caching, normalization and retained replay without contacting providers.

| Example | What to inspect |
|---|---|
| [Nomis](../examples/nomis.md) | String geography codes, numeric interpretation, source status and raw value tokens. |
| [StatsWales](../examples/statswales.md) | Dimension codes, padded values, suppression, notes and unknown units. |
| [English deprivation](../examples/deprivation.md) | Separate measures, geography vintages and reshaping source records into observations. |

The same `Store`, `Request` and `Dataset` API appears in each example. Consult
the generated [source coverage reference](../reference/sources.md) before choosing
published data: a successful fixture is not certification of every provider table.
The [four-nation workflow](four-nations.md) adds NISRA and Scottish selections,
durable job recovery and fresh-process verification after collection transfer.
Use the [common dataset interface](dataset-api.md) to inspect and query provider
observations alongside typed CSV/Parquet records through Python, CLI, REST and MCP.

## Run the combined example

[Install SSDU](../start/install.md), then install the example and documentation
dependencies from the repository root:

```sh
uv sync --frozen --python 3.13 --group docs --extra dev
```

Run all three providers through the checked fixture transport:

```sh
uv run --frozen --offline --extra dev python examples/dataset_contract.py --output-dir data/dataset-contract-example
```

The example verifies each contract, cached reads and explicit offline reads. It
then locks a collection, exports/imports it into a fresh store, and compares the
reopened observation tables, metadata and collection replay. Socket connections
are blocked during the workflow.

Expect `verified` and `fresh_store_equal` to be `true`, with
`provider_network_requests` and `network_attempts` both `0`. The fixture
observation counts are three for Nomis, three for StatsWales and fifteen for
deprivation. Each fixture contains three source rows; deprivation expands each
row into five measures. These are tutorial rows; they do not describe live source coverage.
Fixture HTTP attempts are handled in memory and are distinct from network
requests.

## Execute the notebooks

The downloadable source notebooks accompany the rendered examples. Run their
cells in the locked environment before rebuilding the manual:

Keep the notebooks with this checkout's `examples/dataset_contract.py` helper and
`examples/fixtures/dataset-contract/` fixtures; their imports and synthetic
transport use those repository files.

```sh
uv run --frozen --offline --group docs --extra dev python tools/docs_notebooks.py execute
uv run --frozen --offline --group docs python tools/docs.py build
```

The execution helper supplies repository imports and runs notebooks in fresh
directories. Sphinx renders the checked outputs without running notebook code;
reading the local HTML requires no kernel or provider connection.

To retrieve published observations, inspect the provider's native metadata and
selectors, then use a normal client rather than the fixture client. Fetching a
missing selection or requesting a refresh uses provider networking. Do not
transfer fixture-specific pacing to live use. The fixture client uses a private
limiter; live clients keep their configured provider pacing. Keep source release,
units, missing-value meanings and geography
vintages with the result, and use [offline operation](../concepts/offline.md) for
subsequent retained replay.
