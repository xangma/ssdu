"""Explicit deep-metadata workers and verified offline graph reads."""

import json
from pathlib import Path


def add_arguments(parser):
    commands = parser.add_subparsers(dest="graph_command", required=True)
    commands.add_parser("connectors", help="List metadata-only catalogue integrations")
    commands.add_parser("schema", help="Read the versioned deep-metadata contract")
    plan = commands.add_parser("plan", help="Freeze bounded capture intent without HTTP")
    plan.add_argument("spec", type=Path)
    policy = plan.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--refresh", action="store_true")
    plan.add_argument("--budget", type=Path)
    show = commands.add_parser("plan-show", help="Read a frozen graph capture plan")
    show.add_argument("plan_id")
    submit = commands.add_parser("submit", help="Queue a plan without contacting providers")
    submit.add_argument("plan_id")
    submit.add_argument("--idempotency-key", required=True)
    run = commands.add_parser("run", help="Run or resume one explicit metadata worker")
    run.add_argument("job_id")
    run.add_argument("--max-tasks", type=int, help="Yield after this many operations")
    for name in ("job", "cancel", "retry"):
        command = commands.add_parser(name, help=f"{name.capitalize()} a graph capture job")
        command.add_argument("job_id")
    jobs = commands.add_parser("jobs", help="List durable graph capture jobs")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--offset", type=int, default=0)
    for name in ("verify", "report"):
        command = commands.add_parser(name, help=f"{name.capitalize()} a retained graph")
        command.add_argument("snapshot_id")
    entity = commands.add_parser("entity", help="Read one verified metadata entity")
    entity.add_argument("snapshot_id")
    entity.add_argument("entity_id")
    for name in ("entities", "links"):
        command = commands.add_parser(name, help=f"Page through retained graph {name}")
        command.add_argument("snapshot_id")
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--offset", type=int, default=0)
        if name == "entities":
            command.add_argument("--kind", help="For example variable, question or code_list")
    export = commands.add_parser("export", help="Export a verified metadata-only graph bundle")
    export.add_argument("snapshot_id")
    export.add_argument("--output", type=Path, required=True)
    bundle_import = commands.add_parser("import", help="Verify and import a graph bundle locally")
    bundle_import.add_argument("file", type=Path)


def _read_json(path):
    with path.open("rb") as stream:
        content = stream.read(60_001)
    if len(content) > 60_000:
        raise ValueError("Metadata graph input exceeds the 60 KB input limit")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise TypeError("Metadata graph input must be a JSON object")
    return value


def execute(args):
    command = args.graph_command
    if command == "connectors":
        from ssdu.service import Service

        return Service.list_metadata_connectors()
    if command == "schema":
        from ssdu.metadata_graph import contract_schema

        return contract_schema()
    if command in {"plan", "plan-show", "submit", "run", "job", "jobs", "cancel", "retry"}:
        from ssdu.metadata_graph_capture import MetadataGraphCapture

        capture = MetadataGraphCapture(args.store)
        if command == "plan":
            return capture.plan_capture(
                _read_json(args.spec),
                offline=args.offline,
                refresh=args.refresh,
                budget=_read_json(args.budget) if args.budget else None,
            )
        if command == "plan-show":
            return capture.get_plan(args.plan_id)
        if command == "submit":
            return capture.submit_plan(args.plan_id, args.idempotency_key)
        if command == "run":
            return capture.run_job(args.job_id, max_tasks=args.max_tasks)
        if command == "jobs":
            return capture.list_jobs(limit=args.limit, offset=args.offset)
        return getattr(
            capture, {"job": "get_job", "cancel": "cancel_job", "retry": "retry_job"}[command]
        )(args.job_id)
    from ssdu.metadata_graph_store import MetadataGraphStore

    store = MetadataGraphStore(args.store)
    if command in {"verify", "report"}:
        return getattr(store, command)(args.snapshot_id)
    if command == "entity":
        return store.get_entity(args.snapshot_id, args.entity_id)
    if command == "entities":
        return store.entities(
            args.snapshot_id, kind=args.kind, limit=args.limit, offset=args.offset
        )
    if command == "links":
        return store.links(args.snapshot_id, limit=args.limit, offset=args.offset)
    if command == "export":
        path = store.export(args.snapshot_id, args.output)
        return {"snapshot_id": args.snapshot_id, "bundle": str(path)}
    if command == "import":
        snapshot_id = store.import_bundle(args.file)
        return {"snapshot_id": snapshot_id, "cache": store.seed_cache(snapshot_id)}
    raise ValueError("Unknown metadata graph command")
