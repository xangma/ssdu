# SSDU

SSDU retains UK social-sciences data and local CSV/Parquet tables as immutable
snapshots. Named collections pin queries and interpretation, then transfer their
retained inputs for verified replay. Python, REST and MCP expose local data and
metadata; provider acquisition is explicit.

## Start offline

From this checkout, install the locked environment and run a synthetic example:

```sh
uv sync --frozen --python 3.13
uv run --frozen --offline python examples/documentation_quickstart.py --output data/quickstart
```

Use an absent or empty output directory. The example imports two invented rows,
locks a query, exports a bundle, removes the original CSV and replays in a fresh
store with networking blocked. Success reports `"status": "passed"` and the row
`{"code": "002", "value": 7}`. These are tutorial observations, not official data.

## Read the manual locally

```sh
uv sync --frozen --python 3.13 --group docs --extra dev
uv run --frozen --offline --group docs --extra dev python tools/docs_notebooks.py execute
uv run --frozen --offline --group docs python tools/docs.py build
python -m http.server 8008 --bind 127.0.0.1 --directory data/documentation-build/html
```

Open <http://127.0.0.1:8008/>. The Sphinx/MyST/Furo manual includes an offline
quickstart, provenance guidance and generated CLI, Python, schema, REST and MCP
references. It also explains the standardized `Dataset` contract, renders three
executed provider examples using synthetic responses, and generates source
coverage from the implementation's support matrix. Its build uses the explicit
[publication allowlist](docs/publication.json).
The [four-nation teaching workflow](docs/content/guides/four-nations.md) exercises
scoped NISRA and Scottish datasets alongside English and Welsh examples, with
durable recovery and verified offline transfer. The generated family reference
states the validated shapes; collection membership does not establish comparable
measures across nations.

Notebook preparation requires the checkout's notebooks, `examples/dataset_contract.py`
helper and `examples/fixtures/dataset-contract/` fixtures. It runs in fresh local
directories with provider networking blocked. Re-run preparation after notebook,
fixture, implementation or lock changes. Sphinx checks the saved outputs without
starting kernels.

After notebook preparation, run publication acceptance with:

```sh
uv run --frozen --offline --group docs python tools/docs_acceptance.py
```

The manual has no public deployment. [Documentation maintenance](docs/README.md)
describes staging and checks. Provider datasets, raw metadata and code lists,
research corpora, snapshots and bundles stay in private local working files and
are excluded from source publication. The curated
[research page](docs/content/research/index.md) explains the experimental
discovery work and its pending human validation.

SSDU's software and documentation use the [MIT licence](LICENSE).
Upstream provider material retains its source terms. The
[publication policy](PUBLICATION.md) defines the source-only distribution.
