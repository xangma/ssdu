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

Source coverage Markdown, scope limits, capability flags and downloadable JSON
are generated from `ssdu.source_coverage`; do not maintain a second support list.
The family conformance reference comes from `ssdu.conformance`. Acceptance also
runs the approved four-nation example and checks durable recovery, cache reuse
and fresh-process transfer/replay with network and original-store reads blocked.

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
