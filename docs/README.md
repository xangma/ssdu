# Maintaining the documentation

Reader pages live in `docs/content/`. `publication.json` lists every permitted
page, generated reference, selected Python symbol, schema model and download.
`tools/docs.py` copies that inventory into a clean staging tree and builds only
that tree. New content files must be listed before a build can succeed.

```sh
uv sync --frozen --python 3.13 --group docs --extra dev
uv run --frozen --offline --group docs --extra dev python tools/docs_notebooks.py execute
uv run --frozen --offline --group docs python tools/docs.py build
uv run --frozen --offline --group docs python tools/docs_acceptance.py
```

Both generation and HTML building deny DNS/socket access. For optional external
link checks, first build, then run `tools/docs.py linkcheck`; this command explicitly
contacts linked websites and does not acquire provider datasets. Local link and
anchor validation is part of acceptance.

The manifest maps each approved notebook to a Markdown page and separately
approves its source download. `tools/docs_notebooks.py execute` runs the synthetic
tutorials with private kernels and fresh working directories. Receipts bind
executed cells to source, fixture, implementation, helper and lock hashes. Cached
outputs live in `data/documentation-notebooks/`; rebuild them after an input
changes. Sphinx does not execute notebook code. The renderer emits code and text
outputs only, discards rich HTML representations, and rejects errors, unexecuted
cells, execution overrides, widgets and unsupported assets. Notebook Markdown
uses the same include/download boundary as authored pages.

Provider coverage Markdown, scope limits, capability flags and downloadable JSON
are generated from `ssdu.source_coverage`; do not maintain a second support list.
The `providers` bindings group organisation/service custodians separately from
the `sources` adapter scopes. ONS includes Nomis and planned additional APIs;
MHCLG's implemented scope is one deprivation release file. Local file import is
an input method outside the provider matrix. Provider coverage is partial whenever
any bound access scope is implemented; it never certifies a whole organisation.
Keep existing adapter IDs and source-level capabilities intact when changing
provider presentation.
The family conformance reference comes from `ssdu.conformance`. Acceptance also
runs the approved four-nation example and checks durable recovery, cache reuse
and fresh-process transfer/replay with network and original-store reads blocked.

The dataset API guide is an authored page, not a new notebook. Its
`examples/dataset_api.py` workflow uses invented Nomis responses, a CSV and runtime
Parquet, checks Python/CLI/REST/MCP parity, and transfers a bundle into a fresh
offline process. Run it with `--output-dir` pointing to an absent directory.
Verify its `result.json` before treating the guide's success expectations as
observed results; keep receipts and execution logs under ignored `data/` paths.

Source publication uses the separate root `publication.json`/`PUBLICATION.md`
policy. Build and execute tutorials again inside its clean exported `tree/`:
private research evidence must not be a hidden documentation dependency. All
published fixture values and rendered outputs must be independently invented,
clearly labelled teaching material. MIT covers the software and documentation;
upstream provider material keeps its own terms.

The package reports version 0.3.0; REST/MCP currently advertise 0.2.0. References
display these labels separately. A future version change must be made in its
authoritative source, not in a generated page.

Research sources, frozen bundles and label overlays stay in their existing
directories. Curate any reader-facing research page manually and add it to the
publication manifest. `docs/README.md`, original research documents, development
notes and `data/` receipts never enter the site. Keep dependency installations,
checks and execution logs outside reader prose.

Acceptance rebuilds with warnings treated as errors, compares generated reference
bytes and real CLI help, rejects private source leakage, and checks every local
HTML link, anchor and runtime asset. It also runs the tutorial with networking
denied, removes its source store and transport bundle, and verifies fresh-store
replay and refusal to overwrite an existing run. Boundary tests live in
`tests/test_documentation.py` and need only the core development dependencies.

Before publication, check the rendered manual under a nested URL prefix: search
to quickstart, keyboard skip navigation, theme switching, narrow reference pages
and reflow. Block external browser requests to verify self-contained runtime
assets. Automated receipts and optional browser evidence belong in
`data/documentation-validation/`; they are excluded from the site. Basic browser
checks do not establish assistive-technology or complete accessibility coverage.

## Reader journeys and hosting

The public manual is served at <https://xangma.github.io/ssdu/>. The README and
manual homepage should send new readers to installation and the guided researcher
notebook at `start/research-tutorial.html`, followed by source coverage. Keep
repository setup instructions, package requirements and notebook controls on the
installation page. Local builds, publication mechanics and test receipts belong
here, not in the beginner journey.

The beginner notebook uses invented community-centre counts for fictional wards
in 2023 and 2024. It teaches import, inspection, selection and reopening a saved
snapshot. The existing offline quickstart is the advanced collection-transfer
example. Provider notebooks explain source-specific value meanings, while the
four-nation script demonstrates resumable jobs and transfer. Do not present
synthetic results as official statistics or suggest the four national examples
measure the same thing.

To preview a prepared build locally:

```sh
python -m http.server 8008 --bind 127.0.0.1 --directory data/documentation-build/html
```

Open <http://127.0.0.1:8008/> and stop the server with `Ctrl+C`. GitHub Pages
serves the built manual under `/ssdu/`; verify links, search and runtime assets
under that nested prefix rather than only at a web server's root. Deploy the
approved build through the repository's documentation workflow. GitHub Pages
settings must permit GitHub Actions to publish the site.

## Example checks and research provenance

The three-provider script checks contract consistency, cache reuse and fresh-store
transfer. Its invented observation counts are three for Nomis, three for
StatsWales and fifteen for deprivation: the deprivation example expands three
source records into five measures each. Fixture HTTP attempts are handled in
memory and must not be confused with provider network requests. Teaching clients
use their own limiter; live provider pacing must remain intact.

The dataset API script currently checks 90 comparisons across Python, CLI, REST
and MCP. Its result receipt includes `api_parity_comparisons`,
`provider_network_requests` and `network_attempts`; the transfer process also
denies access to original input and store paths. The four-nation example
interrupts after two retained responses, then resumes persisted job state. Its
receipt records checkpoint reuse, completed-entry reuse and fresh-process checks.
These checks are useful maintenance evidence, not the learning objectives of a
reader-facing guide.

The discovery experiments preserve metadata, questions, evidence references,
ranking settings, splits, vectors and input hashes. Original labels, independent
agent review and agent-adjudicated overlays remain separate versions. The latest
reviewed pool contains 30 questions and 607 candidate pairs; human validation is
pending. Keep these process records in the private research workspace. The public
research page should explain experimental status and interpretation limits
without exposing private corpora, working notes or misleading claims of
human-validated evaluation.
