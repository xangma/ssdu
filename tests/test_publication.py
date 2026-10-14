"""Source publication must not inherit local research or captured provider assets."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "source_publication", Path(__file__).parents[1] / "tools/publication.py"
)
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


def approved(root, name, content, kind="source"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": name,
        "kind": kind,
        "sha256": hashlib.sha256(content).hexdigest(),
        "review": "Independently invented test input; no provider payload.",
    }


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    entries = [
        approved(root, "src/ssdu/example.py", b"value = 1\n"),
        approved(root, "tests/test_example.py", b"def test_example():\n    assert 1 == 1\n"),
        approved(root, "LICENSE", b"MIT License\n", "documentation"),
    ]
    manifest = {
        "schema_version": 1,
        "policy": "source-only",
        "license": "MIT",
        "files": entries,
        "ci": {"test_paths": ["tests/test_example.py"]},
        "excluded_categories": ["captured provider data"],
    }
    (root / "publication.json").write_text(json.dumps(manifest))
    return root


def amend(root, change):
    path = root / "publication.json"
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def test_export_only_copies_approved_bytes_and_preserves_local_data(project):
    canaries = [
        "benchmarks/corpus.json",
        "examples/literature/private.zip",
        "data/raw.csv",
        "src/ssdu/provider-response.json",
        "src/ssdu/.secret.py",
        "DEVELOPMENT.md",
        "tests/new-test.py",
        "docs/content/unreviewed.md",
    ]
    for name in canaries:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PRIVATE_CANARY")
    destination = project / "data/publication/review"
    receipt = publication.export(destination, project)
    assert receipt["unreviewed_files_copied"] == 0
    assert receipt["tests_executed"] is False
    assert receipt["remote_actions"] == []
    assert publication.verify(destination)["status"] == "passed"
    copied = [path for path in (destination / "tree").rglob("*") if path.is_file()]
    assert len(copied) == 4
    assert all(b"PRIVATE_CANARY" not in path.read_bytes() for path in copied)
    assert all((project / name).read_bytes() == b"PRIVATE_CANARY" for name in canaries)


@pytest.mark.parametrize(
    "name",
    [
        "../secret.py",
        "/secret.py",
        "src/../secret.py",
        "src\\secret.py",
        "src//secret.py",
        "./secret.py",
    ],
)
def test_unsafe_paths_fail(project, name):
    amend(project, lambda value: value["files"][0].update(path=name))
    with pytest.raises(ValueError, match="Unsafe"):
        publication.audit(project)


def test_changed_approved_bytes_need_new_review(project):
    (project / "src/ssdu/example.py").write_text("value = 'unreviewed'\n")
    with pytest.raises(ValueError, match="bytes changed"):
        publication.audit(project)


@pytest.mark.parametrize("directory", [False, True])
def test_source_symlinks_rejected(project, directory):
    source = project / ("src/ssdu" if directory else "src/ssdu/example.py")
    moved = project / "original"
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=directory)
    with pytest.raises(ValueError, match="symlink"):
        publication.audit(project)


@pytest.mark.parametrize(
    "name,kind,content",
    [
        ("benchmarks/corpus.json", "synthetic_fixture", b"{}"),
        ("examples/harvest/source.json", "synthetic_fixture", b"{}"),
        ("src/ssdu/response.json", "source", b"{}"),
        ("examples/fixture.parquet", "synthetic_fixture", b"PAR1"),
        ("tests/fixture.json", "synthetic_fixture", b'{"evidence_kind":"source_derived"}'),
        ("src/ssdu/secret.py", "source", b"credential = 'ghp_" + b"x" * 36 + b"'"),
        ("tests/binary.py", "source", b"\x00binary"),
        ("tests/.private.py", "source", b"value = 1\n"),
        ("DEVELOPMENT.md", "documentation", b"private note"),
    ],
)
def test_reclassifying_forbidden_payload_does_not_allow_publication(project, name, kind, content):
    entry = approved(project, name, content, kind)
    amend(project, lambda value: value["files"].append(entry))
    with pytest.raises(ValueError):
        publication.audit(project)


@pytest.mark.parametrize(
    "change",
    [
        lambda cell: cell.update(outputs=[{"output_type": "stream", "text": "private value"}]),
        lambda cell: cell.update(execution_count=1),
        lambda cell: cell.update(attachments={"private.png": {"image/png": "opaque"}}),
    ],
)
def test_notebook_execution_artifacts_are_not_published(project, change):
    cell = {"cell_type": "code", "source": ["print(1)"], "outputs": [], "execution_count": None}
    change(cell)
    notebook = json.dumps({"cells": [cell], "nbformat": 4}).encode()
    entry = approved(project, "examples/notebooks/example.ipynb", notebook, "notebook")
    amend(project, lambda value: value["files"].append(entry))
    with pytest.raises(ValueError, match="no outputs"):
        publication.audit(project)


def test_export_never_overwrites_and_never_writes_into_source(project):
    with pytest.raises(ValueError, match="under data/publication"):
        publication.export(project / "source-export", project)
    destination = project / "data/publication/review"
    publication.export(destination, project)
    with pytest.raises(ValueError, match="must not exist"):
        publication.export(destination, project)


def test_verify_rejects_extra_exported_file_and_corruption(project):
    destination = project / "data/publication/review"
    publication.export(destination, project)
    extra = destination / "tree/src/ssdu/source-response.json"
    extra.write_text("{}")
    with pytest.raises(ValueError, match="extra files"):
        publication.verify(destination)
    extra.unlink()
    (destination / "tree/src/ssdu/example.py").write_text("value = 2\n")
    with pytest.raises(ValueError, match="bytes changed"):
        publication.verify(destination)


def test_unapproved_ci_file_is_rejected(project):
    amend(project, lambda value: value["ci"]["test_paths"].append("tests/private.py"))
    with pytest.raises(ValueError, match="outside approved"):
        publication.audit(project)


def test_receipt_cannot_claim_tests_ran_or_omit_assets(project):
    destination = project / "data/publication/review"
    publication.export(destination, project)
    receipt_path = destination / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(tests_executed=True, file_count=1)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="receipt differs"):
        publication.verify(destination)


def test_duplicate_manifest_keys_fail_closed(project):
    (project / "publication.json").write_text('{"schema_version": 1, "schema_version": 1}')
    with pytest.raises(ValueError, match="Duplicate"):
        publication.audit(project)


def test_real_review_inventory_excludes_unreviewed_research_and_data():
    # Hash freshness is checked by the publication command after coordinated edits.
    manifest = json.loads((Path(__file__).parents[1] / "publication.json").read_text())
    paths = {entry["path"] for entry in manifest["files"]}
    assert all(
        not name.startswith(
            (
                "benchmarks/",
                "data/",
                "examples/literature/",
                "examples/discovery_review/",
                "examples/harvest/",
                "examples/validation/",
            )
        )
        for name in paths
    )
    assert not {"DEVELOPMENT.md", "VALIDATION.md", "DISCOVERY_RESEARCH.md"} & paths
    assert manifest["ci"]["excluded_tests"]
    for entry in manifest["files"]:
        assert entry["review"].strip()
        assert len(entry["sha256"]) == 64
