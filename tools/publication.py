"""Review-bound, source-only exports; never invoke Git or contact a provider."""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "publication.json"
KINDS = {
    "source",
    "configuration",
    "documentation",
    "synthetic_fixture",
    "request_specification",
    "notebook",
}
FORBIDDEN_ROOTS = {"data", "benchmarks", ".git", ".venv", ".env"}
FORBIDDEN_EXAMPLES = {"discovery_review", "literature", "harvest", "validation"}
PRIVATE_NOTES = {
    "AGENTS.md",
    "DEVELOPMENT.md",
    "DISCOVERY_RESEARCH.md",
    "NEXT_STEPS.md",
    "RETRIEVAL_JOBS.md",
    "RETRIEVAL_LAYER.md",
    "SCOPE.md",
    "VALIDATION.md",
}
SECRETS = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{30,}\b"
    r"|\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b"
)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def relative(value):
    if not isinstance(value, str):
        raise TypeError("Publication paths must be strings")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
        or any(part in {"", "."} for part in path.parts)
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"Unsafe publication path: {value!r}")
    return path


def checked_file(root, name):
    path = root / relative(name)
    if root.is_symlink() or any(
        (root / Path(*relative(name).parts[:i])).is_symlink()
        for i in range(1, len(relative(name).parts) + 1)
    ):
        raise ValueError(f"Publication sources must not be symlinks: {name}")
    if not path.is_file():
        raise ValueError(f"Missing regular publication file: {name}")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError(f"Publication file exceeds source size limit: {name}")
    return path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate publication JSON key: {key}")
        result[key] = value
    return result


def read_json(content):
    return json.loads(content, object_pairs_hook=unique_object)


def validate_content(name, kind, content):
    path = relative(name)
    if path.parts[0] in FORBIDDEN_ROOTS or name in PRIVATE_NOTES:
        raise ValueError(f"Forbidden publication category: {name}")
    if path.parts[0] == "examples" and len(path.parts) > 1 and path.parts[1] in FORBIDDEN_EXAMPLES:
        raise ValueError(f"Research assets are excluded: {name}")
    if (
        any(part.startswith(".") for part in path.parts)
        and name != ".gitignore"
        and not name.startswith(".github/workflows/")
    ):
        raise ValueError(f"Unapproved hidden publication path: {name}")
    if kind not in KINDS:
        raise ValueError(f"Unknown publication kind: {kind}")
    suffix = path.suffix
    allowed = {
        "source": suffix == ".py"
        and path.parts[0] in {"src", "tests", "tools", "examples", "docs"},
        "configuration": name
        in {"pyproject.toml", "uv.lock", ".gitignore", "docs/publication.json"}
        or (name.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}),
        "documentation": name in {"README.md", "PUBLICATION.md", "LICENSE", "docs/README.md"}
        or (name.startswith("docs/content/") and suffix == ".md")
        or name == "docs/assets/favicon.svg",
        "synthetic_fixture": suffix in {".json", ".csv", ".yaml", ".yml"}
        and path.parts[0] in {"tests", "examples"},
        "request_specification": suffix in {".json", ".yaml", ".yml"}
        and path.parts[0] == "examples"
        and (len(path.parts) == 2 or path.parts[1] == "collections"),
        "notebook": name.startswith("examples/notebooks/") and suffix == ".ipynb",
    }
    if not allowed[kind]:
        raise ValueError(f"Unapproved asset or file kind: {name}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Binary publication payload: {name}") from exc
    if "\x00" in text or SECRETS.search(text):
        raise ValueError(f"Binary or secret-like publication content: {name}")
    if kind == "synthetic_fixture" and re.search(r'"evidence_kind"\s*:\s*"source_derived"', text):
        raise ValueError(f"Source-derived data cannot be published: {name}")
    if kind == "notebook":
        if any(value in text for value in ("/Users/", "/home/", "/private/", "file://")):
            raise ValueError(f"Notebook contains a local path: {name}")
        notebook = read_json(text)
        for cell in notebook.get("cells", []):
            if (
                cell.get("outputs")
                or cell.get("execution_count") is not None
                or cell.get("attachments")
            ):
                raise ValueError(f"Notebook must have no outputs or attachments: {name}")


def audit(root=ROOT):
    """Validate every approved byte; unlisted files are never implicitly approved."""
    root = Path(root).absolute()
    raw_manifest = checked_file(root, MANIFEST).read_bytes()
    if SECRETS.search(raw_manifest.decode("utf-8")):
        raise ValueError("Secret-like content in publication manifest")
    manifest = read_json(raw_manifest)
    if set(manifest) != {
        "schema_version",
        "policy",
        "license",
        "files",
        "ci",
        "excluded_categories",
    }:
        raise ValueError("Unexpected publication manifest fields")
    if manifest.get("schema_version") != 1 or manifest.get("policy") != "source-only":
        raise ValueError("Unsupported source-only publication manifest")
    if manifest.get("license") != "MIT":
        raise ValueError("Publication software licence must be MIT")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Publication files must be a nonempty exact-file inventory")
    seen = {MANIFEST}
    inventory = []
    contents = {MANIFEST: raw_manifest}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "sha256", "review"}:
            raise ValueError("Publication entry needs path, kind, sha256 and review")
        name = entry["path"]
        relative(name)
        if name in seen:
            raise ValueError(f"Duplicate publication path: {name}")
        seen.add(name)
        if not isinstance(entry["review"], str) or not entry["review"].strip():
            raise ValueError(f"Publication entry needs a review statement: {name}")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(
            "[0-9a-f]{64}", entry["sha256"]
        ):
            raise ValueError(f"Invalid reviewed digest: {name}")
        content = checked_file(root, name).read_bytes()
        if digest(content) != entry["sha256"]:
            raise ValueError(f"Reviewed publication bytes changed: {name}")
        validate_content(name, entry["kind"], content)
        contents[name] = content
        inventory.append(
            {"path": name, "kind": entry["kind"], "sha256": digest(content), "bytes": len(content)}
        )
    ci = manifest.get("ci", {})
    paths = ci.get("test_paths", [])
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Publication CI needs unique test paths")
    for path in paths:
        if path not in seen or not path.startswith("tests/test_") or not path.endswith(".py"):
            raise ValueError(f"CI test is outside approved source tree: {path}")
    inventory.append(
        {
            "path": MANIFEST,
            "kind": "configuration",
            "sha256": digest(raw_manifest),
            "bytes": len(raw_manifest),
        }
    )
    return manifest, sorted(inventory, key=lambda entry: entry["path"]), contents


def receipt_for(manifest, inventory):
    return {
        "schema_version": 1,
        "publication_policy": "source-only",
        "status": "passed",
        "file_count": len(inventory),
        "manifest_sha256": next(
            entry["sha256"] for entry in inventory if entry["path"] == MANIFEST
        ),
        "unreviewed_files_copied": 0,
        "approved_synthetic_assets": sum(
            entry["kind"] == "synthetic_fixture" for entry in inventory
        ),
        "test_paths": manifest["ci"]["test_paths"],
        "tests_executed": False,
        "remote_actions": [],
    }


def export(output, root=ROOT):
    """Atomically stage the reviewed tree; preserve every local excluded file."""
    root = Path(root).absolute()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Publication output must not exist")
    if (
        output == root
        or root in output.parents
        and not output.is_relative_to(root / "data/publication")
    ):
        raise ValueError("In-checkout publication output must be under data/publication")
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise ValueError("Publication output must not use symlinks")
    manifest, inventory, contents = audit(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".publication-", dir=output.parent))
    try:
        tree = staging / "tree"
        tree.mkdir()
        for name, content in contents.items():
            destination = tree / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        receipt = receipt_for(manifest, inventory)
        (staging / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
        (staging / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        verify(staging)
        os.rename(staging, output)
        return receipt
    except BaseException:
        shutil.rmtree(staging)
        raise


def verify(output):
    output = Path(output).absolute()
    tree = output / "tree"
    manifest, inventory, _ = audit(tree)
    declared = read_json(checked_file(output, "inventory.json").read_bytes())
    if inventory != declared:
        raise ValueError("Export inventory differs from verified source tree")
    actual = set()
    for path in tree.rglob("*"):
        if path.is_symlink():
            raise ValueError("Export tree contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(tree).as_posix())
    if actual != {entry["path"] for entry in inventory}:
        raise ValueError("Export tree contains unreviewed extra files")
    receipt = read_json(checked_file(output, "receipt.json").read_bytes())
    if receipt != receipt_for(manifest, inventory):
        raise ValueError("Export receipt differs from the verified publication inventory")
    return {
        "status": "passed",
        "file_count": len(inventory),
        "test_paths": manifest["ci"]["test_paths"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("audit")
    prepare = commands.add_parser("export")
    prepare.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "audit":
        _, inventory, _ = audit(args.root)
        result = {"status": "passed", "file_count": len(inventory)}
    elif args.command == "export":
        result = export(args.output, args.root)
    else:
        result = verify(args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
