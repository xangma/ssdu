# Install

Use a repository checkout and `uv`. Run these commands from the repository root.
They use the repository's dependency lock rather than resolving a new environment.

```sh
uv sync --frozen --python 3.13
uv run --frozen --offline ssdu --help
```

The first command may download Python and dependencies. Once installed, the help
command should print the CLI's commands without contacting a data provider.

The package is named `ssdu-retrieval` and supports Python 3.11 or later. These
instructions use Python 3.13 so the same environment can build the documentation;
the documentation dependency group requires Python 3.12 or later. Repository
installation does not imply that a public package release is available.

## Build and read the manual locally

Install the documentation and validation dependencies once:

```sh
uv sync --frozen --python 3.13 --group docs --extra dev
```

Then build with dependency networking disabled and serve the result locally:

```sh
uv run --frozen --offline --group docs --extra dev python tools/docs_notebooks.py execute
uv run --frozen --offline --group docs python tools/docs.py build
python -m http.server 8008 --bind 127.0.0.1 --directory data/documentation-build/html
```

Open `http://127.0.0.1:8008/`. Stop the local server with `Ctrl+C`. This serves
files on your machine; it does not publish the manual to a hosting service.

The build uses an explicit publication allowlist: selected reader pages,
generated references and approved downloads. Research evidence, maintainer
records and working outputs remain outside that published source tree.

Notebook preparation executes only approved synthetic examples in fresh local
directories, with provider networking blocked. Re-run it after changing notebook
sources, fixtures, implementation or the dependency lock. The HTML build checks
the saved execution receipts and renders text outputs without starting kernels.

## Next

Run the [offline quickstart](quickstart.md). Read the
[offline policy](../concepts/offline.md) before fetching provider data: `uv`'s
`--offline` option governs dependency downloads, while SSDU's applicable
`--offline` options govern provider requests.
