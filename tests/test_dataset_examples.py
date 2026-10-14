"""Execute shipped tutorials in clean processes and private Jupyter kernels."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def environment():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_executable_contract_example_transfers_all_providers_offline(tmp_path):
    output = tmp_path / "acceptance"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "dataset_contract.py"),
            "--output-dir",
            str(output),
        ],
        check=False,
        cwd=tmp_path,
        env=environment(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((output / "result.json").read_bytes())
    assert json.loads(result.stdout) == receipt
    assert receipt["verified"] is True
    assert receipt["fresh_store_equal"] is True
    assert receipt["fixture_kind"] == "synthetic_teaching_fixtures"
    assert receipt["provider_network_requests"] == receipt["network_attempts"] == 0
    assert set(receipt["providers"]) == {"nomis", "statswales", "deprivation"}
    assert {key: value["observation_rows"] for key, value in receipt["providers"].items()} == {
        "nomis": 3,
        "statswales": 3,
        "deprivation": 15,
    }
    assert all(value["cached_requests"] == 0 for value in receipt["providers"].values())


NOTEBOOK_WORKER = r'''
import json
import os
import sys
from pathlib import Path

import nbformat
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient

notebook_path, workdir = Path(sys.argv[1]), Path(sys.argv[2])
# A private kernelspec fixes the interpreter to the actual test environment.
specdir = workdir / "kernels" / "ssdu-test"
specdir.mkdir(parents=True)
(specdir / "kernel.json").write_text(json.dumps({
    "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
    "display_name": "SSDU conformance", "language": "python",
}))
manager = KernelManager(kernel_name="ssdu-test", kernel_spec_manager=KernelSpecManager(
    kernel_dirs=[str(specdir.parent)]
))
nb = nbformat.read(notebook_path, as_version=4)
nbformat.validate(nb)
# This guard spans the whole tutorial, including imports and cache replay.
nb.cells.insert(0, nbformat.v4.new_code_cell("""
import os
from examples.dataset_contract import no_network
_ssdu_rate_settings = {key: os.environ.get(key) for key in (
    "SSDU_RATE_DB", "SSDU_RATE_INTERVAL_SECONDS"
)}
_ssdu_network_guard = no_network()
_ssdu_network_attempts = _ssdu_network_guard.__enter__()
"""))
checks = """
assert _ssdu_network_attempts == []
assert {key: os.environ.get(key) for key in _ssdu_rate_settings} == _ssdu_rate_settings
print("SSDU_NOTEBOOK_VERIFIED")
"""
if nb.metadata.ssdu.get("provider") in {"nomis", "statswales", "deprivation"}:
    checks += "assert cached.snapshot_id == reopened.snapshot_id == dataset.snapshot_id\n"
nb.cells.append(nbformat.v4.new_code_cell(checks))
client = NotebookClient(nb, km=manager, timeout=45, allow_errors=False,
                        resources={"metadata": {"path": str(workdir)}})
client.execute(cwd=str(workdir), env=os.environ.copy())
nbformat.write(nb, workdir / "executed.ipynb")
assert "SSDU_NOTEBOOK_VERIFIED" in "".join(
    output.get("text", "") for cell in nb.cells if cell.cell_type == "code"
    for output in cell.get("outputs", [])
)
print(json.dumps({"verified": True, "provider": nb.metadata.ssdu.get("provider", "local"),
                  "network_attempts": 0}))
'''


@pytest.mark.parametrize("provider", ["nomis", "statswales", "deprivation", "research_tutorial"])
def test_notebook_executes_from_clean_directory_with_network_blocked(provider, tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            NOTEBOOK_WORKER,
            str(ROOT / "examples" / "notebooks" / f"{provider}.ipynb"),
            str(tmp_path),
        ],
        check=False,
        cwd=tmp_path,
        env=environment(),
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "verified": True,
        "provider": provider if provider != "research_tutorial" else "local",
        "network_attempts": 0,
    }
    assert (tmp_path / "executed.ipynb").is_file()
    if provider == "research_tutorial":
        # A later session must reopen the saved table without the input CSV or kernel state.
        (tmp_path / "data/ssdu-tutorial/community-centres.csv").unlink()
        replay = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import json
from pathlib import Path
from examples.dataset_contract import no_network
with no_network() as attempts:
    from ssdu import Store
    saved = Path('data/ssdu-tutorial')
    dataset = Store(saved / 'saved-data').open_dataset(
        (saved / 'snapshot-id.txt').read_text().strip()
    )
    rows = dataset.to_arrow().to_pylist()
    assert len(rows) == 6
    assert next(r for r in rows if r['area'] == 'Meadow Ward' and r['year'] == 2023)[
        'community_centres'
    ] is None
    assert next(r for r in rows if r['area'] == 'Meadow Ward' and r['year'] == 2024)[
        'community_centres'
    ] == 0
    assert 'invented' in dataset.contract['source']['citation']
assert attempts == []
print(json.dumps({'reopened': True, 'network_attempts': 0}))
""",
            ],
            check=False,
            cwd=tmp_path,
            env=environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert replay.returncode == 0, replay.stdout + replay.stderr
        assert json.loads(replay.stdout) == {"reopened": True, "network_attempts": 0}
