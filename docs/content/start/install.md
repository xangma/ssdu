# Install SSDU and open the tutorial

These instructions install SSDU on your computer and open the guided tutorial
in JupyterLab. JupyterLab displays a **notebook**: a document containing
explanations and short pieces of Python that you run one at a time.

The first installation needs an internet connection and may take a few minutes.
The tutorial uses invented data stored on your computer; it does not need a
provider account or download official statistics.

## 1. Prepare your computer

Install these two tools if you do not already have them:

- [Git](https://git-scm.com/downloads) copies the SSDU project onto your computer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installs Python and
  the packages SSDU needs.

Open **Terminal** on macOS or Linux, or **PowerShell** on Windows. This is where
you enter the commands below. Copy each line, press Enter and wait for it to
finish before running the next.

## 2. Download and install SSDU

Choose a folder where you keep projects, then run:

```sh
git clone https://github.com/xangma/ssdu.git
cd ssdu
uv sync --frozen --python 3.13 --extra notebook
```

The first command creates a folder called `ssdu`. The second moves your terminal
into it. The third installs the agreed package versions, including JupyterLab;
`uv` can also download Python 3.13 if needed. Keep this terminal open.

Already have a copy of the project? Open a terminal in its `ssdu` folder and run
only the `uv sync` command.

## 3. Open the notebook

From that same folder, run:

```sh
uv run --frozen --offline --extra notebook jupyter lab examples/notebooks/research_tutorial.ipynb
```

JupyterLab should open in your browser. If it does not, copy the local address
printed in the terminal into your browser. Keep any access token in that address
private. The notebook is called `research_tutorial.ipynb`; open it from the file
list if it is not already visible.

Read the first explanation, click the first code cell, then press
**Shift+Enter** to run it and move to the next cell. Continue in order. A number
beside a cell shows that it has run; an asterisk means it is still running. You
can change a selection and rerun a cell to explore the example.

The [guided tutorial](research-tutorial.md) also shows the steps and expected
results here in the manual.

## Open another lesson

In JupyterLab's file list, open another notebook from `examples/notebooks`:

| Notebook | Task |
| --- | --- |
| `provider_selection.ipynb` | Choose a provider table, area and year. |
| `metadata_discovery.ipynb` | Search and assess a local metadata catalogue. |
| `local_files.ipynb` | Import and query CSV or Parquet files. |

Keep each notebook with any companion `.py` file in that folder. The provider
and metadata lessons use these small helpers to supply invented responses.
The manual offers downloads of both files beside each lesson. The local-file
recipe and first tutorial work without a companion helper.

## Stop and return later

Save the notebook with **Ctrl+S** (or **Cmd+S** on macOS). To stop JupyterLab,
return to the terminal, press **Ctrl+C** and follow its shutdown prompt. To come
back, open a terminal in the `ssdu` folder and repeat the notebook command above.

The `--offline` in that command stops `uv` downloading packages. It does not
prevent all Python code from accessing the internet. This tutorial uses local
data; see [working offline](../concepts/offline.md) before using other workflows.

## Other ways to use SSDU

For command-line help, run:

```sh
uv run --frozen --offline ssdu --help
```

The package is named `ssdu-retrieval` and supports Python 3.11 or later. These
instructions use Python 3.13 and the repository's locked dependencies. They do
not depend on a public package-index release. Application developers can continue
with the [interface guide](../guides/interfaces.md).
