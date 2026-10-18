"""Keep the downloadable notebook kit complete and its execution receipt bound."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "notebook_kits", Path(__file__).resolve().parents[1] / "tools/docs_notebooks.py"
)
kits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kits)


@pytest.fixture
def kit(tmp_path):
    source = "examples/notebooks/lesson.ipynb"
    companion = "examples/notebooks/fixture.py"
    manifest = {
        "notebooks": {"start/lesson.md": source},
        "notebook_companions": {source: [companion]},
        "downloads": {"_downloads/fixture.py": companion},
    }
    files = {
        "docs/publication.json": json.dumps(manifest),
        source: "{}",
        companion: "INVENTED_COUNT = 3\n",
        "examples/dataset_contract.py": "# network guard\n",
        "tools/docs_notebooks.py": "# executor\n",
        "uv.lock": "# lock\n",
    }
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return tmp_path, source, companion, manifest


def test_companion_is_copied_into_fresh_execution_directory_and_hash_bound(kit, tmp_path):
    root, source, companion, _ = kit
    workdir = tmp_path / "fresh"
    workdir.mkdir()
    kits.copy_companions(root, source, workdir)
    assert (workdir / "fixture.py").read_bytes() == (root / companion).read_bytes()
    before = kits.inputs(root, source)
    (root / companion).write_text("INVENTED_COUNT = 4\n")
    assert before[companion] != kits.inputs(root, source)[companion]
    with pytest.raises(ValueError, match="absent"):
        kits.copy_companions(root, source, workdir)


@pytest.mark.parametrize("failure", ["unapproved", "outside", "duplicate", "symlink"])
def test_companion_cannot_escape_the_published_kit(kit, failure):
    root, source, companion, manifest = kit
    if failure == "unapproved":
        manifest["downloads"] = {}
    elif failure == "outside":
        manifest["notebook_companions"][source] = ["examples/private.py"]
    elif failure == "duplicate":
        manifest["notebook_companions"][source] *= 2
    else:
        (root / companion).unlink()
        (root / companion).symlink_to(root / "uv.lock")
    (root / "docs/publication.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        kits.companions(root, source)
