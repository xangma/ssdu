import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError
from ssdu.providers import DEFAULT_PROVIDERS
from ssdu.store import PROVIDERS, Store, _atomic_json, adapter


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _summary(manifest):
    return {
        k: manifest[k]
        for k in (
            "product_id",
            "provider",
            "dataset_id",
            "title",
            "rows",
            "release",
            "created_at",
        )
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="UK data retrieval and frozen snapshot PoC")
    parser.add_argument("--store", default="data", help="Local snapshot directory (default: data)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources", help="Show implemented source coverage and planned integrations")
    dataset = commands.add_parser("dataset", help="Fetch, inspect or migrate standardized datasets")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_commands.add_parser("schema", help="Print the versioned dataset descriptor JSON Schema")
    dataset_fetch = dataset_commands.add_parser("fetch", help="Fetch a request as a common Dataset")
    dataset_fetch.add_argument("request", type=Path)
    dataset_policy = dataset_fetch.add_mutually_exclusive_group()
    dataset_policy.add_argument("--offline", action="store_true")
    dataset_policy.add_argument("--refresh", action="store_true")
    for action in ("show", "migrate"):
        item = dataset_commands.add_parser(
            action,
            help="Open retained standardized data"
            if action == "show"
            else "Normalize historical retained evidence into a new snapshot offline",
        )
        item.add_argument("snapshot_id")
    for item in (
        dataset_fetch,
        *[dataset_commands.choices[action] for action in ("show", "migrate")],
    ):
        item.add_argument(
            "--preview", type=int, default=0, help="Return up to 100 observation rows"
        )
    search = commands.add_parser("search", help="Live provider search (deprivation is curated)")
    search.add_argument("query")
    search.add_argument("--provider", choices=PROVIDERS)
    fetch = commands.add_parser("fetch", help="Fetch a JSON request; cache hit by default")
    fetch.add_argument("request", type=Path)
    fetch_policy = fetch.add_mutually_exclusive_group()
    fetch_policy.add_argument(
        "--refresh", action="store_true", help="Bypass the cache and retrieve current provider data"
    )
    fetch_policy.add_argument(
        "--offline", action="store_true", help="Use only retained bytes; never contact providers"
    )
    cache = commands.add_parser("cache", help="Inspect or repair the local snapshot cache")
    cache.add_argument("--verify", action="store_true", help="Verify retained snapshot hashes")
    cache.add_argument(
        "--rebuild-index", action="store_true", help="Verify snapshots and repair the request index"
    )
    local = commands.add_parser("import-local", help="Import a local table with explicit metadata")
    local.add_argument("file", type=Path)
    local.add_argument("--metadata", type=Path, required=True, help="JSON or YAML import metadata")
    collection = commands.add_parser(
        "collection", help="Lock, replay and transfer named collections"
    )
    collection_commands = collection.add_subparsers(dest="collection_command", required=True)
    lock = collection_commands.add_parser(
        "lock", help="Resolve a JSON/YAML collection specification"
    )
    lock.add_argument("spec", type=Path)
    lock_policy = lock.add_mutually_exclusive_group()
    lock_policy.add_argument("--offline", action="store_true", help="Require retained snapshots")
    lock_policy.add_argument(
        "--refresh", action="store_true", help="Refresh requested provider data"
    )
    collection_commands.add_parser("list", help="List retained collection locks")
    for command in ("show", "replay"):
        sub = collection_commands.add_parser(
            command, help=f"{command.capitalize()} a collection lock"
        )
        sub.add_argument("ref", help="Collection name, full lock ID or lock JSON path")
    export = collection_commands.add_parser(
        "export", help="Export a collection and its retained data"
    )
    export.add_argument("ref", help="Collection name, full lock ID or lock JSON path")
    export.add_argument("--output", required=True, type=Path)
    bundle_import = collection_commands.add_parser(
        "import", help="Import a local collection bundle"
    )
    bundle_import.add_argument("bundle", type=Path)
    bundle_import.add_argument(
        "--activate", action="store_true", help="Replace an existing collection's current lock"
    )
    from ssdu.retrieval_cli import add_jobs_arguments, add_retrieval_arguments

    retrieval = commands.add_parser("retrieval", help="Plan and queue general collection retrieval")
    add_retrieval_arguments(retrieval)
    jobs = commands.add_parser("jobs", help="Inspect and run persistent retrieval jobs")
    add_jobs_arguments(jobs)
    demo = commands.add_parser(
        "demo", help="Retrieve defaults for all three sources and use Intake"
    )
    demo.add_argument("--refresh", action="store_true")
    demo.add_argument("--offline", action="store_true", help="Replay a previous demo; no HTTP")
    commands.add_parser("list", help="List saved snapshots")
    for command in ("verify", "replay"):
        sub = commands.add_parser(
            command,
            help="Verify hashes"
            if command == "verify"
            else "Verify and load saved bytes without HTTP",
        )
        sub.add_argument("product_id")
    catalog = commands.add_parser("catalog", help="Generate an Intake v2 catalogue")
    catalog.add_argument("--output", type=Path)
    commands.add_parser(
        "sync", help="Index retained metadata locally; no provider catalogue harvest"
    )
    benchmark = commands.add_parser(
        "benchmark", help="Replay frozen lexical, semantic and hybrid discovery offline"
    )
    benchmark.add_argument("bundle", type=Path, help="Frozen discovery benchmark directory")
    benchmark.add_argument("--output", type=Path, help="Write the full deterministic JSON report")
    discovery = commands.add_parser(
        "discovery", help="Review discovery, enrich provider metadata and compare offline"
    )
    from ssdu.discovery_cli import add_arguments as add_discovery_arguments

    add_discovery_arguments(discovery)
    literature = commands.add_parser(
        "literature", help="Capture, inspect and transfer paper–dataset evidence"
    )
    from ssdu.literature_cli import add_arguments

    add_arguments(literature)
    harvest = commands.add_parser("harvest", help="Checkpointed metadata-only source harvesting")
    registry = commands.add_parser("registry", help="Offline discovery against retained metadata")
    from ssdu.harvest_cli import add_harvest_arguments, add_registry_arguments

    add_harvest_arguments(harvest)
    add_registry_arguments(registry)
    prepare = commands.add_parser(
        "prepare", help="Prepare shared national deprivation source and registry"
    )
    policy = prepare.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true", help="Require retained bytes; no HTTP")
    policy.add_argument(
        "--refresh", action="store_true", help="Explicitly check the pinned provider asset"
    )
    serve = commands.add_parser("serve", help="Serve read-only REST/OpenAPI on localhost")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--enable-jobs",
        action="store_true",
        help="Enable local retrieval planning and job controls",
    )
    mcp = commands.add_parser("mcp", help="Serve read-only MCP tools and resources")
    mcp.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8001)
    mcp.add_argument(
        "--enable-jobs",
        action="store_true",
        help="Enable local retrieval planning and job controls",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "sources":
            from ssdu.source_coverage import get_source_coverage

            _print(get_source_coverage())
            return 0
        if args.command == "dataset":
            from ssdu.dataset_contract import DatasetDescriptor

            if args.dataset_command == "schema":
                _print(DatasetDescriptor.model_json_schema())
                return 0
            if not 0 <= args.preview <= 100:
                raise ValueError("--preview must be between 0 and 100")
            store = Store(args.store)
            if args.dataset_command == "fetch":
                result = store.fetch_dataset(
                    Request(**json.loads(args.request.read_text())),
                    offline=args.offline,
                    refresh=args.refresh,
                )
            elif args.dataset_command == "show":
                result = store.open_dataset(args.snapshot_id)
            else:
                result = store.migrate_dataset(args.snapshot_id)
            output = {"snapshot_id": result.snapshot_id, "dataset": result.contract}
            if args.preview:
                output["preview"] = result.to_arrow().slice(0, args.preview).to_pylist()
            _print(output)
            return 0
        if args.command in {"retrieval", "jobs"}:
            from ssdu.retrieval_cli import execute

            try:
                result = execute(args)
            except KeyboardInterrupt:
                return 130
            except KeyError as exc:
                raise RetrievalError("Retrieval plan or job not found") from exc
            if result is not None:
                _print(result)
            return 0
        if args.command == "discovery":
            from ssdu.discovery_cli import execute

            _print(execute(args))
            return 0
        if args.command in {"harvest", "registry"}:
            from ssdu.harvest_cli import execute

            _print(execute(args))
            return 0
        if args.command == "literature":
            from ssdu.literature_cli import execute

            _print(execute(args))
            return 0
        if args.command == "benchmark":
            from ssdu.discovery_benchmark import run_benchmark

            if args.output and args.bundle.resolve() in args.output.resolve().parents:
                raise ValueError("Write benchmark reports outside the frozen input bundle")
            report = run_benchmark(args.bundle)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                )
            _print(report)
            return 0
        store = Store(args.store)
        if args.command == "search":
            results = []
            for provider in [args.provider] if args.provider else PROVIDERS:
                with HttpClient() as client:
                    results.extend(adapter(provider).search(args.query, client))
            _print(results)
        elif args.command == "fetch":
            request = Request(**json.loads(args.request.read_text()))
            manifest = store.fetch(request, refresh=args.refresh, offline=args.offline)
            product_id = manifest["product_id"]
            catalogue = store.catalogue([product_id], store.root / f"catalog-{product_id}.yaml")
            _print({**_summary(manifest), "catalogue": str(catalogue)})
        elif args.command == "cache":
            report = (
                store.rebuild_index()
                if args.rebuild_index
                else store.cache_status(verify=args.verify)
            )
            _print(report)
            return 1 if report.get("invalid_snapshots") or report.get("index_errors") else 0
        elif args.command == "import-local":
            from ssdu.collections import read_document
            from ssdu.local_import import import_local

            manifest = import_local(store, args.file, read_document(args.metadata))
            product_id = manifest["product_id"]
            catalogue = store.catalogue([product_id], store.root / f"catalog-{product_id}.yaml")
            _print({**_summary(manifest), "catalogue": str(catalogue)})
        elif args.command == "collection":
            from ssdu.collections import Collections, read_document

            collections = Collections(store)
            if args.collection_command == "lock":
                locked = collections.resolve(
                    read_document(args.spec), offline=args.offline, refresh=args.refresh
                )
                _print(locked.model_dump())
            elif args.collection_command == "list":
                _print(collections.list())
            elif args.collection_command == "show":
                _print(collections.get(args.ref).model_dump())
            elif args.collection_command == "replay":
                _print(collections.replay(args.ref))
            elif args.collection_command == "export":
                from ssdu.bundles import export_bundle

                _print(export_bundle(store, collections.get(args.ref), args.output))
            elif args.collection_command == "import":
                from ssdu.bundles import import_bundle

                _print(import_bundle(store, args.bundle, activate=args.activate))
        elif args.command == "demo":
            if args.offline and args.refresh:
                raise ValueError("--offline and --refresh cannot be combined")
            demo_path = store.root / "demo.json"
            manifests = []
            for provider in DEFAULT_PROVIDERS:
                print(f"Resolving {provider}…", file=sys.stderr, flush=True)
                manifests.append(
                    store.fetch(
                        adapter(provider).default_request(),
                        refresh=args.refresh,
                        offline=args.offline,
                    )
                )
            products = []
            for manifest in manifests:
                product_id = manifest["product_id"]
                frame = store.replay(product_id)
                loaded = store.read_intake(product_id)
                pd.testing.assert_frame_equal(frame, loaded)
                products.append({**_summary(manifest), "intake_roundtrip": "passed"})
            catalogue = store.catalogue([m["product_id"] for m in manifests])
            result = {
                "product_ids": [m["product_id"] for m in manifests],
                "products": products,
                "catalogue": str(catalogue),
                "offline": args.offline,
            }
            _atomic_json(demo_path, result)
            _print(result)
        elif args.command == "list":
            _print([_summary(m) for m in store.list()])
        elif args.command == "verify":
            _print({"verified": True, **_summary(store.verify(args.product_id))})
        elif args.command == "replay":
            frame = store.replay(args.product_id)
            print(frame.head(10).to_string(index=False))
            print(f"\n{len(frame)} rows × {len(frame.columns)} columns; hashes verified; no HTTP")
        elif args.command == "catalog":
            _print({"catalogue": str(store.catalogue(output=args.output))})
        elif args.command == "sync":
            from ssdu.registry import Registry

            registry = Registry(store.root)
            count = registry.import_store(store)
            _print(
                {
                    "snapshots_indexed": count,
                    "quarantined": registry.last_import_errors,
                    "catalogue": registry.search(),
                }
            )
        elif args.command == "prepare":
            from ssdu.registry import Registry
            from ssdu.shared import SharedDeprivation

            manifest = SharedDeprivation(store).source(refresh=args.refresh, offline=args.offline)
            registry = Registry(store.root)
            registry.import_store(store)
            _print(
                {
                    **_summary(manifest),
                    "kind": manifest["kind"],
                    "catalogue": str(store.catalogue()),
                    "registry": str(registry.path),
                }
            )
        elif args.command == "serve":
            import uvicorn

            from ssdu.api import create_app

            options = {"enable_jobs": True} if args.enable_jobs else {}
            uvicorn.run(create_app(store.root, **options), host=args.host, port=args.port)
        elif args.command == "mcp":
            from ssdu.mcp_server import main as mcp_main

            mcp_main(
                [
                    "--store",
                    str(store.root),
                    "--transport",
                    args.transport,
                    "--host",
                    args.host,
                    "--port",
                    str(args.port),
                ]
                + (["--enable-jobs"] if args.enable_jobs else [])
            )
        return 0
    except (RetrievalError, ValueError, OSError, TypeError) as exc:
        print(f"ssdu: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
