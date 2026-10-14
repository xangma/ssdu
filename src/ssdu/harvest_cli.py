"""Explicit acquisition and offline registry operations for standalone users."""

from pathlib import Path

from ssdu.literature_cli import _read
from ssdu.literature_store import LiteratureStore
from ssdu.registry import Registry


def _policy(parser):
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--refresh", action="store_true")


def add_harvest_arguments(parser):
    commands = parser.add_subparsers(dest="harvest_command", required=True)
    rich = commands.add_parser("rich-context", help="Import the complete pinned Rich Context graph")
    rich.add_argument("--from-snapshot", help="Reuse raw corpus/licence in a verified snapshot")
    rich.add_argument("--source-store", type=Path)
    _policy(rich)
    for name in ("literature", "providers"):
        sub = commands.add_parser(name, help="Run a bounded JSON metadata harvest plan")
        sub.add_argument("plan", type=Path)
        _policy(sub)
    commands.add_parser("list", help="List verified evidence/metadata snapshots")
    for name in ("show", "verify", "index"):
        sub = commands.add_parser(name)
        sub.add_argument("snapshot_id")
    export = commands.add_parser("export")
    export.add_argument("snapshot_id")
    export.add_argument("--output", type=Path, required=True)
    imported = commands.add_parser("import")
    imported.add_argument("bundle", type=Path)


def add_registry_arguments(parser):
    commands = parser.add_subparsers(dest="registry_command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--provider", choices=("nomis", "statswales", "deprivation", "local"))
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    describe = commands.add_parser("describe")
    describe.add_argument("dataset_id")


def execute(args):
    if args.command == "registry":
        registry = Registry(args.store)
        if args.registry_command == "search":
            return registry.search(args.query, args.provider, args.limit, args.offset)
        return registry.get(args.dataset_id)
    command = args.harvest_command
    store = LiteratureStore(args.store)
    if command == "rich-context":
        from ssdu.rich_context_harvest import import_rich_context

        return import_rich_context(
            args.store,
            from_snapshot=args.from_snapshot,
            source_store=args.source_store,
            offline=args.offline,
            refresh=args.refresh,
        )
    if command in {"literature", "providers"}:
        plan = _read(args.plan)[0]
        if command == "literature":
            from ssdu.uk_literature import harvest_uk_literature

            return harvest_uk_literature(
                args.store, plan, offline=args.offline, refresh=args.refresh
            )
        from ssdu.provider_harvest import harvest_provider_metadata

        result = harvest_provider_metadata(
            args.store, plan, offline=args.offline, refresh=args.refresh
        )
        return {
            **result,
            "registry": Registry(args.store).import_catalogue(store, result["snapshot_id"]),
        }
    if command == "list":
        return store.list()
    if command == "show":
        return store.read(args.snapshot_id)
    if command == "verify":
        return store.verify(args.snapshot_id)
    if command == "index":
        return Registry(args.store).import_catalogue(store, args.snapshot_id)
    if command == "export":
        return store.export(args.snapshot_id, args.output)
    if command == "import":
        snapshot_id = store.import_bundle(args.bundle)
        result = {"snapshot_id": snapshot_id, "counts": store.verify(snapshot_id)["counts"]}
        if "catalogue_descriptors" in store.read(snapshot_id):
            result["registry"] = Registry(args.store).import_catalogue(store, snapshot_id)
        return result
    raise ValueError("Unknown metadata harvest operation")
