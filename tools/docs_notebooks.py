"""Execute approved synthetic notebooks separately; render checked text outputs only."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = "data/documentation-notebooks"
PREPARE = "python tools/docs_notebooks.py execute"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(root, name):
    from pathlib import PurePosixPath

    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name:
        raise ValueError(f"Unsafe notebook path: {name}")
    target = root / name
    if not target.is_file() or any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError(f"Notebook input must be a regular, nonsymlink file: {name}")
    return target


def inputs(root, source):
    """Conservative freshness binding to tutorials, fixtures, implementation and lock."""
    names = {source, "examples/dataset_contract.py", "tools/docs_notebooks.py", "uv.lock"}
    for directory, pattern in (
        ("src/ssdu", "*.py"),
        ("examples", "*.py"),
        ("examples/fixtures", "*"),
        ("examples/collections", "*"),
    ):
        names.update(
            p.relative_to(root).as_posix() for p in (root / directory).rglob(pattern) if p.is_file()
        )
    return {name: digest(regular(root, name)) for name in sorted(names)}


def text(value):
    return "".join(value) if isinstance(value, list) else value


def validate(nb, executed=False):
    if nb.get("nbformat") != 4 or not nb.get("cells"):
        raise ValueError("Expected a nonempty version 4 notebook")
    serialized = json.dumps(nb)
    if any(private in serialized for private in ("/Users/", "/home/", "/private/", "file://")):
        raise ValueError("Private path in notebook")

    def metadata(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "mystnb",
                    "nbsphinx",
                    "widgets",
                    "execution",
                    "execute",
                    "execution_mode",
                }:
                    raise ValueError("Notebook execution/widget metadata is not permitted")
                metadata(child)
        elif isinstance(value, list):
            for child in value:
                metadata(child)

    metadata(nb.get("metadata", {}))
    for cell in nb["cells"]:
        metadata(cell.get("metadata", {}))
        if cell.get("attachments") or cell["cell_type"] not in {"markdown", "code"}:
            raise ValueError("Only Markdown/code notebook cells without attachments are supported")
        if not isinstance(text(cell.get("source")), str):
            raise TypeError("Notebook cell source must be text")
        if cell["cell_type"] != "code":
            continue
        if executed and type(cell.get("execution_count")) is not int:
            raise ValueError("Notebook has an unexecuted code cell")
        if not executed and (cell.get("outputs") or cell.get("execution_count") is not None):
            raise ValueError("Source notebooks must have cleared outputs")
        for output in cell.get("outputs", []):
            kind = output.get("output_type")
            if kind == "stream":
                if not isinstance(text(output.get("text")), str):
                    raise ValueError("Invalid stream output")
            elif kind in {"execute_result", "display_data"}:
                data = output.get("data", {})
                if "text/plain" not in data or set(data) - {"text/plain", "text/html"}:
                    raise ValueError("Notebook requires unsupported active or binary output")
            else:
                raise ValueError("Notebook has an error or unsupported output")


def fence(value, language):
    length = max([3, *(len(match[0]) + 1 for match in re.finditer(r"`+", value))])
    delimiter = "`" * length
    return f"\n{delimiter}{language}\n{value.rstrip()}\n{delimiter}\n"


def render(root, page, source):
    original = json.loads(regular(root, source).read_text())
    validate(original)
    folder = root / CACHE / Path(source).stem
    try:
        receipt = json.loads(regular(root, f"{CACHE}/{folder.name}/receipt.json").read_text())
        notebook_path = regular(root, f"{CACHE}/{folder.name}/executed.ipynb")
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Prepare notebook outputs first: {PREPARE}") from exc
    if (
        receipt.get("inputs") != inputs(root, source)
        or receipt.get("executed_sha256") != digest(notebook_path)
        or receipt.get("verified") is not True
        or receipt.get("network_attempts") != 0
    ):
        raise ValueError(f"Stale or invalid notebook receipt; run {PREPARE}")
    nb = json.loads(notebook_path.read_text())
    validate(nb, executed=True)
    identity = lambda cells: [(c["cell_type"], text(c["source"]), c.get("id")) for c in cells]
    if identity(nb["cells"]) != identity(original["cells"]):
        raise ValueError("Executed notebook cells differ from approved source")
    rendered = []
    for cell in nb["cells"]:
        value = text(cell["source"])
        if cell["cell_type"] == "markdown":
            rendered.append(value + "\n")
            continue
        rendered.append(fence(value, "python"))
        for output in cell.get("outputs", []):
            value = text(
                output["text"]
                if output["output_type"] == "stream"
                else output["data"]["text/plain"]
            )
            rendered.append(fence(value, "text"))
    download = os.path.relpath(f"_downloads/{Path(source).name}", Path(page).parent).replace(
        os.sep, "/"
    )
    rendered.insert(
        1,
        f"\n{{download}}`Download this notebook <{download}>`\n",
    )
    return "\n".join(rendered)


def worker(source, workdir):
    import nbformat
    from jupyter_client import KernelManager
    from jupyter_client.kernelspec import KernelSpecManager
    from nbclient import NotebookClient

    specdir = workdir / "kernels/ssdu-docs"
    specdir.mkdir(parents=True)
    (specdir / "kernel.json").write_text(
        json.dumps(
            {
                "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                "display_name": "SSDU documentation",
                "language": "python",
            }
        )
    )
    manager = KernelManager(
        kernel_name="ssdu-docs",
        kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(specdir.parent)]),
    )
    nb = nbformat.read(source, as_version=4)
    validate(nb)
    nbformat.validate(nb)
    nb.cells.insert(
        0,
        nbformat.v4.new_code_cell(
            "from examples.dataset_contract import no_network\n"
            "_ssdu_docs_guard = no_network()\n"
            "_ssdu_docs_attempts = _ssdu_docs_guard.__enter__()"
        ),
    )
    checks = "assert _ssdu_docs_attempts == []\n"
    if nb.metadata.get("ssdu", {}).get("provider") in {"nomis", "statswales", "deprivation"}:
        checks += "assert cached.snapshot_id == reopened.snapshot_id == dataset.snapshot_id\n"
    checks += "print('SSDU_NOTEBOOK_VERIFIED')"
    nb.cells.append(nbformat.v4.new_code_cell(checks))
    NotebookClient(nb, km=manager, timeout=45, allow_errors=False, record_timing=False).execute(
        cwd=str(workdir), env=os.environ.copy()
    )
    assert any(
        "SSDU_NOTEBOOK_VERIFIED" in output.get("text", "") for output in nb.cells[-1].outputs
    )
    nb.cells = nb.cells[1:-1]
    validate(nb, executed=True)
    nbformat.write(nb, workdir / "executed.ipynb")


def execute(root=ROOT):
    manifest = json.loads((root / "docs/publication.json").read_text())
    receipts = {}
    for page, source in manifest["notebooks"].items():
        regular(root, source)
        folder = root / CACHE / Path(source).stem
        if any(p.is_symlink() for p in [folder, *folder.parents]):
            raise ValueError("Notebook cache cannot use symlinks")
        if folder.exists():
            if folder.is_symlink():
                raise ValueError("Notebook cache cannot be a symlink")
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        binding = inputs(root, source)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root), str(root / "src")))}
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_worker",
                str(root / source),
                str(folder),
            ],
            check=False,
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        (folder / "execution.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Notebook failed: {source}; see {folder / 'execution.log'}")
        if binding != inputs(root, source):
            raise ValueError("Notebook inputs changed during execution; rerun preparation")
        receipt = {
            "verified": True,
            "network_attempts": 0,
            "inputs": binding,
            "executed_sha256": digest(folder / "executed.ipynb"),
        }
        (folder / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        render(root, page, source)
        receipts[page] = {
            "verified": True,
            "network_attempts": 0,
            "executed_sha256": receipt["executed_sha256"],
        }
        print(f"Verified {source}", flush=True)
    return receipts


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["execute"])
    parser.parse_args()
    print(json.dumps(execute(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
