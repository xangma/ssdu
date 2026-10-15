"""Publication boundaries must fail closed before Sphinx sees any content."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "docs_build", Path(__file__).parents[1] / "tools/docs.py"
)
docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    (tmp_path / "docs/content").mkdir(parents=True)
    (tmp_path / "docs/content/index.md").write_text("# Reader page\n")
    (tmp_path / "docs/conf.py").write_text("project = 'fixture'\n")
    manifest = {
        "schema_version": 1,
        "pages": ["index.md"],
        "generated_pages": [],
        "generated_assets": [],
        "downloads": {},
        "python_symbols": [],
        "schema_models": [],
    }
    (tmp_path / "docs/publication.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(docs, "references", lambda manifest: {})
    return tmp_path


def test_only_allowlisted_sources_enter_stage(publication):
    (publication / "PRIVATE.md").write_text("Private research and development diary")
    (publication / "docs/README.md").write_text("Maintainer notes")
    (publication / "data").mkdir()
    (publication / "data/private.json").write_text("{}")
    staged = docs.stage(publication / "data/documentation-test", publication)
    assert {p.name for p in staged.iterdir()} == {"index.md", "conf.py"}


def test_unlisted_reader_page_is_rejected(publication):
    (publication / "docs/content/private.md").write_text("Private diary")
    with pytest.raises(ValueError, match="allowlist"):
        docs.inventory(publication)


@pytest.mark.parametrize("source", ["examples/other.py", "../private.py"])
def test_dataset_api_acceptance_rejects_unapproved_executable(publication, source):
    manifest = publication / "docs/publication.json"
    value = json.loads(manifest.read_text())
    value["dataset_api_example"] = source
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Dataset API.*independently allowlisted"):
        docs.inventory(publication)


def test_dataset_api_acceptance_requires_approved_download(publication):
    source = publication / "examples/dataset_api.py"
    source.parent.mkdir()
    source.write_text("raise RuntimeError('must not execute during inventory')\n")
    manifest = publication / "docs/publication.json"
    value = json.loads(manifest.read_text())
    value["dataset_api_example"] = "examples/dataset_api.py"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Dataset API.*independently allowlisted"):
        docs.inventory(publication)
    value["downloads"]["_downloads/dataset_api.py"] = "examples/dataset_api.py"
    manifest.write_text(json.dumps(value))
    assert docs.inventory(publication) == value


def test_symlink_source_is_rejected(publication):
    page = publication / "docs/content/index.md"
    page.unlink()
    (publication / "private.md").write_text("Private diary")
    page.symlink_to(publication / "private.md")
    with pytest.raises(ValueError, match="nonsymlink"):
        docs.inventory(publication)


@pytest.mark.parametrize("path", ["../private.md", "/private.md", "a/../private.md", "a\\b.md"])
def test_manifest_path_cannot_escape(path):
    with pytest.raises(ValueError, match="Unsafe"):
        docs.relative(path)


@pytest.mark.parametrize(
    "directive",
    [
        "```{literalinclude} ../../PRIVATE.md\n```",
        "```{eval-rst}\n.. include:: /private\n```",
        "```{autoclass} ssdu.store.Store\n```",
        "![private](../../PRIVATE.png)",
        "{download}`private <../../PRIVATE.md>`",
    ],
)
def test_reader_directive_cannot_expand_publication(publication, directive):
    (publication / "docs/content/index.md").write_text(directive)
    with pytest.raises(ValueError, match="escapes|reserved"):
        docs.stage(publication / "data/documentation-test", publication)


def test_cleanup_cannot_target_repository(publication):
    with pytest.raises(ValueError, match="Build output"):
        docs.stage(publication, publication)


def test_network_guard_is_effective():
    import socket

    with docs.offline(), pytest.raises(RuntimeError, match="no network"):
        socket.create_connection(("localhost", 80))


def test_generated_references_include_real_connector_capabilities_without_opening_store(
    monkeypatch,
):
    from ssdu.service import Service

    def forbidden(*args, **kwargs):
        pytest.fail("Reference generation opened a data service")

    monkeypatch.setattr(Service, "__init__", forbidden)
    with docs.offline():
        generated = docs.references(docs.inventory())
    for filename in ("openapi.json", "openapi-jobs.json"):
        api = json.loads(generated[f"_downloads/{filename}"])
        assert len([path for path in api["paths"] if path.startswith("/providers/")]) == 3
    expected = {
        "list_provider_datasets",
        "describe_provider_dataset",
        "list_provider_dimension_values",
    }
    for filename in ("mcp.json", "mcp-jobs.json"):
        inventory = json.loads(generated[f"_downloads/{filename}"])
        assert expected <= {tool["name"] for tool in inventory["tools"]}
    with pytest.raises(AssertionError, match="must not execute"):
        docs.ReferenceService().describe_provider_dataset("mhclg", "synthetic")


@pytest.fixture
def notebook(publication, monkeypatch):
    helper = docs.docs_notebooks
    source = "examples/notebooks/fixture.ipynb"
    page = "examples/fixture.md"
    original = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "markdown", "id": "intro", "metadata": {}, "source": "# Fixture\n"},
            {
                "cell_type": "code",
                "id": "example",
                "metadata": {},
                "source": "print('offline')",
                "execution_count": None,
                "outputs": [],
            },
        ],
    }
    original_path = publication / source
    original_path.parent.mkdir(parents=True)
    original_path.write_text(json.dumps(original))
    manifest_path = publication / "docs/publication.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["notebooks"] = {page: source}
    manifest["downloads"]["_downloads/fixture.ipynb"] = source
    manifest_path.write_text(json.dumps(manifest))
    cache = publication / helper.CACHE / "fixture"
    cache.mkdir(parents=True)
    executed = deepcopy(original)
    executed["cells"][1].update(
        execution_count=2,
        outputs=[
            {
                "output_type": "display_data",
                "metadata": {},
                "data": {"text/plain": "offline", "text/html": "<b>discarded rich output</b>"},
            },
        ],
    )
    output = cache / "executed.ipynb"
    output.write_text(json.dumps(executed))
    monkeypatch.setattr(helper, "inputs", lambda root, name: {name: helper.digest(root / name)})
    receipt = {
        "verified": True,
        "network_attempts": 0,
        "inputs": helper.inputs(publication, source),
        "executed_sha256": helper.digest(output),
    }
    (cache / "receipt.json").write_text(json.dumps(receipt))
    return publication, page, source, executed, cache


def test_notebook_stages_code_text_and_separate_source_download(notebook):
    root, page, source, _, _ = notebook
    staged = docs.stage(root / "data/documentation-test", root)
    rendered = (staged / page).read_text()
    assert "```python\nprint('offline')" in rendered
    assert "```text\noffline" in rendered
    assert "discarded rich output" not in rendered
    assert (staged / "_downloads/fixture.ipynb").read_bytes() == (root / source).read_bytes()


@pytest.mark.parametrize("change", ["source", "output", "receipt"])
def test_notebook_requires_current_execution_receipt(notebook, change):
    root, page, source, _, cache = notebook
    if change == "source":
        path = root / source
        path.write_text(path.read_text().replace("print('offline')", "print('changed')"))
    elif change == "output":
        path = cache / "executed.ipynb"
        path.write_text(path.read_text().replace("offline", "tampered"))
    else:
        (cache / "receipt.json").unlink()
    with pytest.raises(ValueError, match="receipt|Prepare"):
        docs.docs_notebooks.render(root, page, source)


@pytest.mark.parametrize("change", ["error", "unexecuted", "widget", "override", "private", "raw"])
def test_invalid_notebook_output_is_rejected(notebook, change):
    _, _, _, nb, _ = notebook
    if change == "error":
        nb["cells"][1]["outputs"] = [{"output_type": "error", "ename": "Failure"}]
    elif change == "unexecuted":
        nb["cells"][1]["execution_count"] = None
    elif change == "widget":
        nb["cells"][1]["outputs"][0]["data"]["application/vnd.jupyter.widget-view+json"] = {}
    elif change == "override":
        nb["metadata"]["mystnb"] = {"execution_mode": "force"}
    elif change == "private":
        nb["metadata"]["cwd"] = "/Users/private/research"
    else:
        nb["cells"][0]["cell_type"] = "raw"
    with pytest.raises(ValueError):
        docs.docs_notebooks.validate(nb, executed=True)


def test_notebook_markdown_cannot_include_private_source(notebook):
    root, _page, source, nb, cache = notebook
    nb["cells"][0]["source"] = "```{include} ../../PRIVATE.md\n```"
    original = deepcopy(nb)
    original["cells"][1].update(execution_count=None, outputs=[])
    (root / source).write_text(json.dumps(original))
    output = cache / "executed.ipynb"
    output.write_text(json.dumps(nb))
    helper = docs.docs_notebooks
    receipt = {
        "verified": True,
        "network_attempts": 0,
        "inputs": helper.inputs(root, source),
        "executed_sha256": helper.digest(output),
    }
    (cache / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="escapes"):
        docs.stage(root / "data/documentation-test", root)


def test_notebook_download_must_be_allowlisted(notebook):
    root, _, _, _, _ = notebook
    path = root / "docs/publication.json"
    manifest = json.loads(path.read_text())
    manifest["downloads"] = {}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="independently allowlisted"):
        docs.inventory(root)


def test_notebook_source_symlink_is_rejected(notebook):
    root, _, source, _, _ = notebook
    path = root / source
    private = root / "private.ipynb"
    path.rename(private)
    path.symlink_to(private)
    with pytest.raises(ValueError, match="nonsymlink"):
        docs.inventory(root)


@pytest.mark.parametrize(
    "changed_input",
    [
        "examples/four_nations.py",
        "examples/fixtures/four-nations/nomis.json",
        "examples/collections/four-nations.yaml",
    ],
)
def test_notebook_receipt_rejects_changed_extra_helper_or_fixture(
    notebook, monkeypatch, changed_input
):
    root, page, source, _, cache = notebook
    # Exercise the actual binding rather than the small fixture's input stub.
    monkeypatch.undo()
    for name in (
        "tools/docs_notebooks.py",
        "uv.lock",
        "examples/dataset_contract.py",
        "examples/four_nations.py",
        "examples/fixtures/four-nations/nomis.json",
        "examples/collections/four-nations.yaml",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original input\n")
    helper = docs.docs_notebooks
    receipt = {
        "verified": True,
        "network_attempts": 0,
        "inputs": helper.inputs(root, source),
        "executed_sha256": helper.digest(cache / "executed.ipynb"),
    }
    (cache / "receipt.json").write_text(json.dumps(receipt))
    assert "offline" in helper.render(root, page, source)
    (root / changed_input).write_text("changed input\n")
    with pytest.raises(ValueError, match="Stale"):
        helper.render(root, page, source)
