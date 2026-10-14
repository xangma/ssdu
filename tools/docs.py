"""Stage an explicit publication inventory and generate references from SSDU itself."""

import argparse
import asyncio
import importlib
import inspect
import json
import re
import shutil
import socket
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import docs_notebooks

DEFAULT_OUTPUT = ROOT / "data/documentation-build"


@contextmanager
def offline():
    """Fail immediately if generation or Sphinx attempts a network connection."""

    def denied(*args, **kwargs):
        raise RuntimeError("Documentation generation/build permits no network access")

    with (
        patch.object(socket, "getaddrinfo", denied),
        patch.object(socket, "create_connection", denied),
        patch.object(socket.socket, "connect", denied),
        patch.object(socket.socket, "connect_ex", denied),
        patch.object(socket.socket, "sendto", denied),
    ):
        yield


def relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Unsafe publication path: {value}")
    if str(path) != value:
        raise ValueError(f"Noncanonical publication path: {value}")
    return value


def checked_file(root, name):
    path = root / relative(name)
    if not path.is_file() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError(f"Publication source must be a regular, nonsymlink file: {name}")
    return path


def inventory(root=ROOT):
    manifest = json.loads(checked_file(root / "docs", "publication.json").read_text())
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported publication manifest")
    names = manifest["pages"] + manifest["generated_pages"] + manifest["generated_assets"]
    names += list(manifest["downloads"])
    notebooks = manifest.get("notebooks", {})
    names += list(notebooks)
    for name in names:
        relative(name)
    if len(names) != len(set(names)):
        raise ValueError("Duplicate publication paths")
    if any(
        not p.endswith(".md")
        for p in manifest["pages"] + manifest["generated_pages"] + list(notebooks)
    ):
        raise ValueError("Reader pages must be Markdown")
    actual = {
        p.relative_to(root / "docs/content").as_posix()
        for p in (root / "docs/content").rglob("*")
        if p.is_file()
    }
    if actual != set(manifest["pages"]):
        raise ValueError(
            f"Content differs from allowlist: {sorted(actual ^ set(manifest['pages']))}"
        )
    for name in manifest["pages"]:
        checked_file(root / "docs/content", name)
    for name in manifest["downloads"].values():
        checked_file(root, name)
    if len(set(notebooks.values())) != len(notebooks):
        raise ValueError("Duplicate notebook sources")
    for source in notebooks.values():
        if not source.endswith(".ipynb") or not source.startswith("examples/notebooks/"):
            raise ValueError("Notebook source must be an approved example notebook")
        checked_file(root, source)
        if manifest["downloads"].get(f"_downloads/{Path(source).name}") != source:
            raise ValueError("Notebook download must be independently allowlisted")
    for key, expected, label in (
        ("four_nation_example", "examples/four_nations.py", "Four-nation"),
        ("dataset_api_example", "examples/dataset_api.py", "Dataset API"),
    ):
        example = manifest.get(key)
        if example is not None:
            if (
                example != expected
                or manifest["downloads"].get(f"_downloads/{Path(expected).name}") != example
            ):
                raise ValueError(f"{label} acceptance example must be independently allowlisted")
            checked_file(root, example)
    return manifest


def symbol(name):
    parts = name.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError as exc:
            if exc.name != ".".join(parts[:split]):
                raise
            continue
        for part in parts[split:]:
            obj = getattr(obj, part)
        return obj
    raise ValueError(f"Cannot import {name}")


def cli_inventory():
    """Capture the real argparse tree before dispatch; never execute a command."""
    from ssdu.cli import main

    class Captured(Exception):
        pass

    captured = []

    def capture(parser, *args, **kwargs):
        captured.append(parser)
        raise Captured

    with (
        patch.object(argparse.ArgumentParser, "parse_args", capture),
        patch.object(sys, "argv", ["ssdu"]),
        patch.dict("os.environ", {"COLUMNS": "80"}),
    ):
        try:
            main([])
        except Captured:
            pass
    result = []

    def visit(parser, command):
        result.append({"command": command, "help": parser.format_help()})
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child, f"{command} {name}")

    visit(captured[0], "ssdu")
    return result


async def mcp_inventory(enable_jobs=False):
    from mcp import Client

    from ssdu.mcp_server import create_mcp_server

    # Registration/listing must never read a store or call service methods.
    server = create_mcp_server(service=object(), enable_jobs=enable_jobs)
    async with Client(server) as client:
        tools = await client.list_tools()
        templates = await client.list_resource_templates()
        return {
            "version": server.version,
            "tools": [t.model_dump(mode="json", by_alias=True) for t in tools.tools],
            "resource_templates": [
                t.model_dump(mode="json", by_alias=True) for t in templates.resource_templates
            ],
        }


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def code(value, language="json"):
    # A longer fence safely contains ordinary triple-backtick docstrings/help.
    return f"\n````{language}\n{value.rstrip()}\n````\n"


def references(manifest):
    from ssdu import __version__
    from ssdu.api import create_app
    from ssdu.conformance import conformance_coverage
    from ssdu.source_coverage import get_source_coverage, source_coverage_markdown

    cli = cli_inventory()
    apis = [
        create_app(service=object(), enable_jobs=enabled).openapi() for enabled in (False, True)
    ]
    mcps = [asyncio.run(mcp_inventory(enabled)) for enabled in (False, True)]
    schemas = {name: symbol(name).model_json_schema() for name in manifest["schema_models"]}
    files = {
        "_downloads/cli.json": json_text(cli),
        "_downloads/openapi.json": json_text(apis[0]),
        "_downloads/openapi-jobs.json": json_text(apis[1]),
        "_downloads/mcp.json": json_text(mcps[0]),
        "_downloads/mcp-jobs.json": json_text(mcps[1]),
        "_downloads/schemas.json": json_text(schemas),
    }
    text = "# CLI reference\n\nGenerated from the installed CLI parser. Global `--store` precedes the command.\n"
    text += "\n{download}`Parser help inventory <../_downloads/cli.json>`\n"
    for item in cli:
        text += f"\n## `{item['command']}`\n" + code(item["help"], "text")
    files["reference/cli.md"] = text
    text = "# Python reference\n\nSelected public entry points, generated from installed signatures and docstrings.\n"
    text += "\nSee the [quickstart](../start/quickstart.md) for a complete local example.\n"
    for name in manifest["python_symbols"]:
        obj = symbol(name)
        directive = (
            "autoclass"
            if inspect.isclass(obj)
            else (
                "automethod"
                if name.rsplit(".", 1)[0].rsplit(".", 1)[-1][:1].isupper()
                else "autofunction"
            )
        )
        text += f"\n## `{name}`\n\n```{{{directive}}} {name}\n```\n"
    files["reference/python.md"] = text
    text = "# Schema reference\n\nGenerated Pydantic JSON Schemas retain bounds, defaults and required fields.\n"
    text += "\n{download}`Complete schema inventory <../_downloads/schemas.json>`\n"
    for name, schema in schemas.items():
        text += f"\n## `{name}`\n" + code(json_text(schema))
    files["reference/schemas.md"] = text
    text = f"# REST reference\n\nPackage version: {__version__}. OpenAPI version label: {apis[0]['info']['version']}.\n"
    text += "\nGenerated from `create_app().openapi()`. `/health` is included. Successful responses with an empty schema have no declared response model; no stronger contract is implied.\n"
    for enabled, api in zip((False, True), apis):
        label = "Optional local retrieval controls" if enabled else "Default reads"
        filename = "openapi-jobs.json" if enabled else "openapi.json"
        text += f"\n## {label}\n\n{{download}}`OpenAPI JSON <../_downloads/{filename}>`\n"
        if enabled:
            text += "\nEnable with `ssdu serve --enable-jobs`. These routes persist local plans/jobs; the server does not run a worker.\n"
        for path, methods in api["paths"].items():
            if enabled and path in apis[0]["paths"]:
                continue
            for method, operation in methods.items():
                if method not in {"get", "post", "put", "delete", "patch"}:
                    continue
                text += f"\n### `{method.upper()} {path}`\n\n{operation.get('summary', '')}\n"
                text += code(
                    json_text(
                        {
                            k: operation[k]
                            for k in ("parameters", "requestBody", "responses")
                            if k in operation
                        }
                    )
                )
    files["reference/rest.md"] = text
    text = f"# MCP reference\n\nPackage version: {__version__}. Server version label: {mcps[0]['version']}.\n"
    text += "\nGenerated with the SDK's in-process client from registered tool and resource schemas. No service method is invoked.\n"
    for enabled, mcp in zip((False, True), mcps):
        filename = "mcp-jobs.json" if enabled else "mcp.json"
        label = "Optional local retrieval controls" if enabled else "Default reads"
        text += f"\n## {label}\n\n{{download}}`MCP inventory <../_downloads/{filename}>`\n"
        if enabled:
            text += "\nEnable with `ssdu mcp --enable-jobs`. Submission and cancellation change local state; no embedded worker runs.\n"
        default_names = {t["name"] for t in mcps[0]["tools"]}
        for tool in mcp["tools"]:
            if enabled and tool["name"] in default_names:
                continue
            text += f"\n### `{tool['name']}`\n\n{tool.get('description', '')}\n"
            text += code(
                json_text(
                    {
                        k: v
                        for k, v in tool.items()
                        if k not in {"name", "description"} and v is not None
                    }
                )
            )
    text += "\n## Resource templates\n" + code(json_text(mcps[0]["resource_templates"]))
    files["reference/mcp.md"] = text
    coverage = get_source_coverage()
    files["_downloads/source-coverage.json"] = json_text(coverage)
    text = "# Source coverage\n\nGenerated from SSDU's declared support matrix.\n\n"
    versions = ", ".join(f"`{version}`" for version in coverage["supported_contract_versions"])
    text += (
        f"Coverage schema: `{coverage['schema_version']}`. "
        f"Latest dataset contract: `{coverage['dataset_contract_version']}`. "
        f"Supported contracts: {versions}. Each source row identifies its profile.\n\n"
    )
    text += "{download}`Complete coverage matrix <../_downloads/source-coverage.json>`\n\n"
    text += "See [family conformance](conformance.md) for the exercised shapes and their protocol references.\n\n"
    text += source_coverage_markdown()
    for source in coverage["sources"]:
        text += f"\n## {source['name']}\n\n{source['access']}\n\n"
        text += "\n".join(f"- {limit}" for limit in source["limits"]) + "\n"
        text += (
            "\n### Declared capabilities\n\n| Capability | Implemented within scope |\n|---|---|\n"
        )
        text += (
            "\n".join(
                f"| `{key}` | {'yes' if value else 'no'} |"
                for key, value in source["capabilities"].items()
            )
            + "\n"
        )
    text += "\n## Planned adapter families\n\n"
    for family in coverage["planned_adapter_families"]:
        text += f"- `{family['family_id']}` ({family['status']}): {family['scope']}\n"
    files["reference/sources.md"] = text
    conformance = conformance_coverage()
    files["_downloads/conformance.json"] = json_text(conformance)
    text = "# Family conformance\n\nGenerated from SSDU's registered semantic test families. "
    text += "These bounded tests do not certify every dataset in a provider portal or comparability across nations.\n\n"
    text += f"{conformance['fixture_policy']}\n\n"
    text += "{download}`Conformance registry <../_downloads/conformance.json>`\n"
    for family in conformance["families"]:
        text += f"\n## `{family['family_id']}`\n\nProvider: `{family['provider']}`. Evidence: `{family['evidence_kind']}`.\n\n{family['scope']}\n\n"
        text += f"Automated check: `{family['test_node']}`.\n\n"
        text += (
            "Protocol references: "
            + ", ".join(f"[reference {i}]({url})" for i, url in enumerate(family["references"], 1))
            + ".\n"
        )
    files["reference/conformance.md"] = text
    expected = set(manifest["generated_pages"] + manifest["generated_assets"])
    if set(files) != expected:
        raise ValueError("Generator output differs from publication manifest")
    return files


def validate_directives(stage, manifest):
    """Only staged files may be included; author pages cannot expand autodoc scope."""
    allowed = set(manifest["pages"] + manifest["generated_pages"])
    allowed |= set(manifest.get("notebooks", {}))
    allowed |= set(manifest["generated_assets"]) | set(manifest["downloads"])
    for name in manifest["pages"] + list(manifest.get("notebooks", {})):
        text = (stage / name).read_text()
        if "eval-rst" in text or re.search(r"\{auto\w+\}", text):
            raise ValueError(
                f"Autodoc/raw reStructuredText is reserved for generated references: {name}"
            )
        targets = [
            m[2].strip()
            for m in re.finditer(r"\{(include|literalinclude|image|figure)\}\s+([^\n]+)", text)
        ]
        targets += re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        targets += re.findall(r"\{download\}`[^`<]*<([^>]+)>`", text)
        for target in targets:
            resolved = (stage / name).parent / target
            try:
                included = resolved.resolve().relative_to(stage.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError(f"Include escapes publication tree: {name}") from exc
            if included not in allowed:
                raise ValueError(f"Unapproved include: {name}: {target}")


def stage(output=DEFAULT_OUTPUT, root=ROOT):
    manifest = inventory(root)
    # Keep destructive cleanup strictly within a dedicated, ignored build directory.
    output = Path(output).resolve()
    data = (root / "data").resolve()
    if output == data or data not in output.parents or not output.name.startswith("documentation-"):
        raise ValueError("Build output must be data/documentation-* within the repository")
    destination = output / "source"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name in manifest["pages"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked_file(root / "docs/content", name), target)
    shutil.copyfile(checked_file(root / "docs", "conf.py"), destination / "conf.py")
    for name, source in manifest["downloads"].items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked_file(root, source), target)
    with offline():
        for name, content in references(manifest).items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for name, source in manifest.get("notebooks", {}).items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(docs_notebooks.render(root, name, source), encoding="utf-8")
    validate_directives(destination, manifest)
    return destination


def build(output=DEFAULT_OUTPUT):
    source = stage(output)
    html = Path(output) / "html"
    if html.exists():
        shutil.rmtree(html)
    from sphinx.cmd.build import build_main

    with offline():
        status = build_main(
            [
                "-n",
                "-W",
                "--keep-going",
                "-b",
                "html",
                "-d",
                str(Path(output) / "doctrees"),
                "-E",
                "-a",
                str(source),
                str(html),
            ]
        )
    if status:
        raise RuntimeError(f"Strict Sphinx build failed ({status})")
    return html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stage", "build", "linkcheck"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.command == "build":
        print(build(args.output))
    elif args.command == "stage":
        print(stage(args.output))
    else:
        # Deliberately online: opt-in checking of external websites, after staging.
        from sphinx.cmd.build import build_main

        source = stage(args.output)
        raise SystemExit(
            build_main(["-W", "-b", "linkcheck", str(source), str(args.output / "linkcheck")])
        )


if __name__ == "__main__":
    main()
