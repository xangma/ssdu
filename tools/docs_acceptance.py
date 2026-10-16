"""Run deterministic publication, contract, link and fresh-store acceptance checks."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import docs


class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.ids = set()
        self.links = []
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for attr in ("href", "src"):
            if attr in attrs:
                self.links.append((tag, attr, attrs[attr]))


def local_links(html):
    pages = {p: Page(p) for p in html.rglob("*.html")}
    checked = 0
    for path, page in pages.items():
        for tag, attr, link in page.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                # Hyperlinks may cite external evidence; runtime assets must be local.
                assert tag == "a" and attr == "href", f"External runtime asset: {link}"
                continue
            assert not url.path.startswith("/"), f"Root-relative URL breaks nested hosting: {link}"
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            assert target.is_relative_to(html.resolve()), f"Link escapes site: {path}: {link}"
            assert target.is_file(), f"Missing local target: {path}: {link}"
            if url.fragment and target in pages:
                assert unquote(url.fragment) in pages[target].ids, f"Missing anchor: {path}: {link}"
            checked += 1
    return {"html_pages": len(pages), "local_links_and_assets": checked}


def main():
    output = docs.DEFAULT_OUTPUT
    validation = docs.ROOT / "data/documentation-validation"
    validation.mkdir(parents=True, exist_ok=True)
    manifest = docs.inventory()
    # A build consumes checked outputs, never launches a kernel or another process.
    with patch.object(subprocess, "Popen", side_effect=AssertionError("Build started a process")):
        html = docs.build()
    source = output / "source"
    names = manifest["pages"] + manifest["generated_pages"] + manifest["generated_assets"]
    names += list(manifest["downloads"]) + ["conf.py"]
    names += list(manifest.get("notebooks", {}))
    assert {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()} == set(
        names
    )
    assert not list((html / "_sources").rglob("*"))
    expected = {
        Path(name).with_suffix(".html").as_posix()
        for name in manifest["pages"]
        + manifest["generated_pages"]
        + list(manifest.get("notebooks", {}))
    }
    actual = {p.relative_to(html).as_posix() for p in html.rglob("*.html")}
    assert expected | {"search.html", "genindex.html"} <= actual
    assert actual <= expected | {"search.html", "genindex.html", "py-modindex.html"}
    for path in [*html.rglob("*.html"), html / "searchindex.js", *html.rglob("*.json")]:
        text = path.read_text(encoding="utf-8")
        for private in ("/Users/", "documentation-review/", "NEXT_STEPS.md", "VALIDATION.md"):
            assert private not in text, f"Private material in {path.name}: {private}"
    with docs.offline():
        repeated = docs.references(manifest)
    for name, content in repeated.items():
        assert (source / name).read_text(encoding="utf-8") == content, f"Unstable reference: {name}"
    from ssdu import get_source_coverage
    from ssdu.conformance import conformance_coverage

    assert (
        json.loads((source / "_downloads/source-coverage.json").read_text())
        == get_source_coverage()
    )
    families = conformance_coverage()
    assert json.loads((source / "_downloads/conformance.json").read_text()) == families
    family_ids = {item["family_id"] for item in families["families"]}
    assert all(
        set(row["conformance_family_ids"]) <= family_ids for row in get_source_coverage()["sources"]
    )
    notebooks = {}
    for page, notebook in manifest.get("notebooks", {}).items():
        rendered = docs.docs_notebooks.render(docs.ROOT, page, notebook)
        assert (source / page).read_text() == rendered, f"Unstable notebook rendering: {page}"
        assert (source / f"_downloads/{Path(notebook).name}").read_bytes() == (
            docs.ROOT / notebook
        ).read_bytes()
        notebooks[page] = {
            "verified": True,
            "network_attempts": 0,
            "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        }

    cli = json.loads((source / "_downloads/cli.json").read_text())
    env = {**os.environ, "COLUMNS": "80"}
    for item in cli:
        result = subprocess.run(
            ["ssdu", *item["command"].split()[1:], "--help"],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
        assert result.stdout == item["help"], f"CLI help mismatch: {item['command']}"

    run = validation / "quickstart"
    if run.exists():
        shutil.rmtree(run)
    result = subprocess.run(
        [
            sys.executable,
            str(docs.ROOT / "examples/documentation_quickstart.py"),
            "--output",
            str(run),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    quickstart = json.loads(result.stdout)
    assert quickstart["status"] == "passed" and quickstart["original_input_removed"]
    assert not (run / "notes.csv").exists()
    # Replay must stand alone after both the source store and transport are removed.
    shutil.rmtree(run / "source")
    (run / "notes.ssdu.zip").unlink()
    with docs.offline():
        from ssdu.collections import Collections
        from ssdu.store import Store

        replayed = Collections(Store(run / "fresh")).replay("notes")
    assert replayed["entries"]["observations"]["rows"] == quickstart["rows"]
    rejected = subprocess.run(
        [
            sys.executable,
            str(docs.ROOT / "examples/documentation_quickstart.py"),
            "--output",
            str(run),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0 and "absent or empty" in rejected.stderr
    four_nations = None
    if example := manifest.get("four_nation_example"):
        run = validation / "four-nations"
        if run.exists():
            shutil.rmtree(run)
        result = subprocess.run(
            [sys.executable, str(docs.ROOT / example), "--output-dir", str(run)],
            cwd=docs.ROOT,
            check=True,
            text=True,
            capture_output=True,
            timeout=150,
        )
        four_nations = json.loads(result.stdout)
        assert four_nations == json.loads((run / "result.json").read_text())
        assert four_nations["verified"] and four_nations["fresh_store_verified"]
        assert four_nations["fixture_kind"] == "synthetic_teaching_fixtures"
        assert four_nations["worker_restarted"] and four_nations["job_status"] == "succeeded"
        assert four_nations["retained_responses_reused"] == 2
        assert four_nations["completed_entries_reused"] == ["england"]
        assert set(four_nations["nations"]) == {"england", "wales", "scotland", "northern_ireland"}
        assert four_nations["network_attempts"] == 0
        destination = four_nations["destination"]
        assert (
            destination["verified"]
            and destination["network_attempts"] == destination["source_store_attempts"] == 0
        )
        assert all(row["cached_requests"] == 0 for row in four_nations["nations"].values())
        assert all(
            row["raw_equal"] and row["normalized_equal"] and row["intake_equal"]
            for row in destination["providers"].values()
        )
        assert four_nations["live_rate_settings_unchanged"]
    dataset_api = None
    if example := manifest.get("dataset_api_example"):
        run = validation / "dataset-api"
        if run.exists():
            shutil.rmtree(run)
        result = subprocess.run(
            [sys.executable, str(docs.ROOT / example), "--output-dir", str(run)],
            cwd=docs.ROOT,
            check=True,
            text=True,
            capture_output=True,
            timeout=240,
        )
        dataset_api = json.loads(result.stdout)
        assert dataset_api == json.loads((run / "result.json").read_text())
        assert dataset_api["verified"] and dataset_api["fresh_store_verified"]
        assert dataset_api["fixture_kind"] == "synthetic_teaching_fixtures"
        assert dataset_api["interfaces"] == ["python", "cli", "rest", "mcp"]
        assert dataset_api["api_parity_comparisons"] == 90
        assert dataset_api["network_attempts"] == dataset_api["provider_network_requests"] == 0
        assert dataset_api["live_rate_settings_unchanged"]
        assert {
            name: (row["profile"], row["contract_version"])
            for name, row in dataset_api["datasets"].items()
        } == {
            "nomis": ("observations", "1.0.0"),
            "local_csv": ("records", "1.1.0"),
            "local_parquet": ("records", "1.1.0"),
        }
        destination = dataset_api["destination"]
        assert destination["interfaces"] == dataset_api["interfaces"]
        assert destination["comparisons"] == 45
        assert destination["pid"] != dataset_api["pid"]
        assert destination["cli_pid"] != dataset_api["cli_pid"]
        assert destination["network_attempts"] == destination["original_input_attempts"] == 0
        assert all(
            destination[key]
            for key in (
                "verified",
                "original_paths_unavailable",
                "dataset_hashes_equal",
                "queries_equal",
                "metadata_equal",
                "collection_replay_equal",
            )
        )
        evidence = json.loads((run / "expected.json").read_text())
        for item in evidence["results"].values():
            for dimension in item["describe"]["descriptor"]["dimensions"]:
                assert "codes" not in dimension
                assert dimension["codes_omitted"] and isinstance(dimension["code_count"], int)
        unchanged = (run / "result.json").read_bytes()
        rejected = subprocess.run(
            [sys.executable, str(docs.ROOT / example), "--output-dir", str(run)],
            cwd=docs.ROOT,
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert rejected.returncode != 0 and "FileExistsError" in rejected.stderr
        assert (run / "result.json").read_bytes() == unchanged
    metadata_capture = None
    if example := manifest.get("metadata_capture_example"):
        run = validation / "metadata-capture"
        if run.exists():
            shutil.rmtree(run)
        result = subprocess.run(
            [sys.executable, str(docs.ROOT / example), "--output-dir", str(run)],
            cwd=docs.ROOT,
            check=True,
            text=True,
            capture_output=True,
            timeout=150,
        )
        metadata_capture = json.loads(result.stdout)
        assert metadata_capture == json.loads((run / "result.json").read_text())
        assert metadata_capture["status"] == "passed"
        assert metadata_capture["records"] == metadata_capture["search_results"] == 10
        assert metadata_capture["provider_network_requests"] == 0
        assert metadata_capture["fresh_store_replayed_offline"]
        assert metadata_capture["normalized_fields_and_evidence_equal"]
        assert metadata_capture["index_rebuilt_offline"]
        assert metadata_capture["original_store_path_unavailable"]
    default_rest = json.loads((source / "_downloads/openapi.json").read_text())
    jobs_rest = json.loads((source / "_downloads/openapi-jobs.json").read_text())
    default_mcp = json.loads((source / "_downloads/mcp.json").read_text())
    jobs_mcp = json.loads((source / "_downloads/mcp-jobs.json").read_text())
    assert len(default_rest["paths"]) == 19 and len(jobs_rest["paths"]) == 37
    assert sum(len(methods) for methods in jobs_rest["paths"].values()) == 40
    assert {
        "/snapshots/{snapshot_id}/dataset": "get",
        "/snapshots/{snapshot_id}/dataset/query": "post",
        "/snapshots/{snapshot_id}/dataset/dimensions/{dimension}": "get",
        "/providers/{provider}/datasets": "get",
        "/providers/{provider}/datasets/{dataset_id}": "get",
        "/providers/{provider}/datasets/{dataset_id}/dimensions/{dimension}": "get",
        "/metadata/datasets": "get",
        "/metadata/status": "get",
        "/metadata/snapshots": "get",
        "/metadata/captures/{capture_id}": "get",
    }.items() <= {
        (path, method) for path, methods in default_rest["paths"].items() for method in methods
    }
    assert len(default_mcp["tools"]) == 18 and len(jobs_mcp["tools"]) == 39
    dataset_tools = {
        "describe_snapshot",
        "query_dataset",
        "list_snapshot_dimension_values",
        "list_provider_datasets",
        "describe_provider_dataset",
        "list_provider_dimension_values",
        "search_metadata",
        "describe_metadata",
        "metadata_status",
        "list_metadata_snapshots",
        "describe_metadata_snapshot",
        "metadata_capture_report",
    }
    assert dataset_tools <= {tool["name"] for tool in default_mcp["tools"]}
    assert all(
        tool["annotations"]["readOnlyHint"]
        for tool in default_mcp["tools"]
        if tool["name"] in dataset_tools
    )
    assert len(default_mcp["resource_templates"]) == 3
    assert "ssdu://snapshots/{snapshot_id}/dataset" in {
        resource["uriTemplate"] for resource in default_mcp["resource_templates"]
    }
    assert {f"ssdu dataset {command}" for command in ("describe", "query", "dimensions")} <= {
        item["command"] for item in cli
    }
    # CLI/replay checks take time; reject input changes during acceptance too.
    for page, notebook in manifest.get("notebooks", {}).items():
        assert (source / page).read_text() == docs.docs_notebooks.render(docs.ROOT, page, notebook)
        assert (source / f"_downloads/{Path(notebook).name}").read_bytes() == (
            docs.ROOT / notebook
        ).read_bytes()
    receipt = {
        "status": "passed",
        "published": False,
        "allowlisted_pages": len(expected),
        "cli_help_checks": len(cli),
        "default_rest_paths": len(default_rest["paths"]),
        "jobs_rest_paths": len(jobs_rest["paths"]),
        "jobs_rest_operations": sum(len(v) for v in jobs_rest["paths"].values()),
        "optional_rest_paths": len(jobs_rest["paths"]) - len(default_rest["paths"]),
        "optional_rest_operations": sum(len(v) for v in jobs_rest["paths"].values())
        - sum(len(v) for v in default_rest["paths"].values()),
        "default_mcp_tools": len(default_mcp["tools"]),
        "jobs_mcp_tools": len(jobs_mcp["tools"]),
        "optional_mcp_tools": len(jobs_mcp["tools"]) - len(default_mcp["tools"]),
        "mcp_resource_templates": len(default_mcp["resource_templates"]),
        "network_denied_during_generation_build_and_quickstart": True,
        "fresh_replay_without_original_store_or_bundle": True,
        "quickstart": quickstart,
        "notebooks": notebooks,
        "build_started_no_processes": True,
        "source_coverage_matches_core": True,
        "conformance_registry_matches_core": True,
        "conformance_families": len(families["families"]),
        "four_nations": four_nations,
        "dataset_api": dataset_api,
        "metadata_capture": metadata_capture,
        "links": local_links(html),
        "generated_sha256": {
            name: hashlib.sha256(content.encode()).hexdigest() for name, content in repeated.items()
        },
    }
    (validation / "acceptance.json").write_text(docs.json_text(receipt))
    print(docs.json_text(receipt))


if __name__ == "__main__":
    main()
