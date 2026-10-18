"""Downloaded teaching notebooks run without repository-only helpers or a live service."""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import nbformat
import pytest

from ssdu.http import HttpClient

NOTEBOOKS = Path(__file__).resolve().parents[1] / "examples" / "notebooks"


@pytest.fixture
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("A synthetic teaching notebook attempted network access")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(HttpClient, "request", denied)


def execute_cells(notebook, *, stop_at=None):
    namespace = {"__name__": "__main__"}
    for cell in notebook.cells:
        if cell.cell_type == "code":
            exec(compile(cell.source, f"notebook:{cell.id}", "exec"), namespace)  # noqa: S102
        if cell.id == stop_at:
            break
    return namespace


@pytest.mark.parametrize("filename", ["research_tutorial.ipynb", "local_files.ipynb"])
def test_reader_notebook_runs_from_an_empty_working_directory(
    filename, tmp_path, monkeypatch, deny_network
):
    monkeypatch.chdir(tmp_path)
    notebook = nbformat.read(NOTEBOOKS / filename, as_version=4)
    nbformat.validate(notebook)
    namespace = execute_cells(notebook)
    # Opening the retained version must preserve its typed table and descriptions.
    original = namespace.get("dataset", namespace.get("csv_dataset"))
    reopened = namespace["reopened"]
    assert reopened.to_arrow().equals(original.to_arrow())
    assert reopened.contract == original.contract


def test_beginner_reopens_in_a_new_process_without_the_original_csv(
    tmp_path, monkeypatch, deny_network
):
    monkeypatch.chdir(tmp_path)
    notebook = nbformat.read(NOTEBOOKS / "research_tutorial.ipynb", as_version=4)
    namespace = execute_cells(notebook, stop_at="research-save-id")
    namespace["source_file"].unlink()
    reopen = next(cell.source for cell in notebook.cells if cell.id == "research-reopen")
    # This is precisely the standalone cell a returning reader runs. A fresh
    # interpreter has none of the setup/import/query variables from the lesson.
    guard = """
import socket
from ssdu.http import HttpClient

def denied(*args, **kwargs):
    raise AssertionError("Reopening attempted network access")

socket.getaddrinfo = socket.create_connection = denied
socket.socket.connect = socket.socket.connect_ex = denied
HttpClient.request = denied
"""
    checks = """
assert reopened.to_arrow().num_rows == 6
assert reopened.to_arrow().column("community_centres").to_pylist() == [4, 5, 2, 3, None, 0]
assert reopened.contract["source"]["citation"].startswith("SSDU teaching example:")
"""
    process = subprocess.run(
        [sys.executable, "-c", guard + "\n" + reopen + "\n" + checks],
        cwd=tmp_path,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "Meadow Ward" in process.stdout


@pytest.mark.parametrize("filename", ["research_tutorial.ipynb", "local_files.ipynb"])
def test_downloaded_notebook_links_and_source_are_portable(filename):
    notebook = nbformat.read(NOTEBOOKS / filename, as_version=4)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            assert cell.execution_count is None
            assert cell.outputs == []
            assert "from examples" not in cell.source
            assert "sys.path" not in cell.source
        else:
            links = re.findall(r"\]\(([^)]+)\)", cell.source)
            assert all(link.startswith("https://xangma.github.io/ssdu/") for link in links)
            assert all(".md" not in link for link in links)


@pytest.mark.parametrize("filename", ["nomis.ipynb", "statswales.ipynb", "deprivation.ipynb"])
def test_provider_notebook_links_work_outside_the_rendered_manual(filename):
    notebook = nbformat.read(NOTEBOOKS / filename, as_version=4)
    links = [
        link
        for cell in notebook.cells
        if cell.cell_type == "markdown"
        for link in re.findall(r"\]\(([^)]+)\)", cell.source)
    ]
    assert links
    assert all(link.startswith("https://xangma.github.io/ssdu/") for link in links)
    assert all(".md" not in link for link in links)
