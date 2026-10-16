"""Metadata snapshots, offline discovery, durable sync controls and explicit workers."""

from pathlib import Path


def add_arguments(parser):
    commands = parser.add_subparsers(dest="metadata_command", required=True)
    capture = commands.add_parser(
        "capture", help="Capture complete provider catalogues in resumable bounded batches"
    )
    from ssdu.metadata_capture_cli import add_arguments as add_capture_arguments

    add_capture_arguments(capture)
    plan = commands.add_parser("plan", help="Freeze metadata sync intent without provider access")
    plan.add_argument("spec", type=Path, help="MetadataSyncSpec JSON file")
    policy = plan.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--refresh", action="store_true")
    plan.add_argument("--budget", type=Path, help="JobBudget JSON file")
    show = commands.add_parser("plan-show", help="Read a frozen metadata plan")
    show.add_argument("plan_id")
    submit = commands.add_parser(
        "submit", help="Queue a metadata sync plan without provider access"
    )
    submit.add_argument("plan_id")
    submit.add_argument("--idempotency-key", required=True)
    run = commands.add_parser(
        "run", help="Run one due metadata job; provider access may be required"
    )
    run.add_argument("job_id", nargs="?")
    for name in ("job", "cancel", "retry"):
        command = commands.add_parser(name, help=f"{name.capitalize()} a retained metadata job")
        command.add_argument("job_id")
    for name in ("jobs", "snapshots"):
        command = commands.add_parser(name, help=f"List retained metadata {name}")
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--offset", type=int, default=0)
    for name in ("snapshot", "verify", "index"):
        command = commands.add_parser(
            name, help=f"{name.capitalize()} a retained metadata snapshot"
        )
        command.add_argument("snapshot_id")
    export = commands.add_parser("export", help="Export a verified portable metadata bundle")
    export.add_argument("snapshot_id")
    export.add_argument("--output", type=Path, required=True)
    bundle_import = commands.add_parser("import", help="Verify and import a local metadata bundle")
    bundle_import.add_argument("file", type=Path)
    search = commands.add_parser("search", help="Search the local metadata index offline")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--provider")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--include-retired", action="store_true")
    describe = commands.add_parser("describe", help="Read current discovery metadata offline")
    describe.add_argument("dataset_id")
    describe.add_argument("--snapshot-id", help="Read an exact retained metadata snapshot")
    status = commands.add_parser("status", help="Report metadata freshness and coverage scope")
    status.add_argument("--provider")
    status.add_argument("--max-age-seconds", type=int, default=86400)


def _read_json(path, model):
    with path.open("rb") as stream:
        raw = stream.read(60_001)
    if len(raw) > 60_000:
        raise ValueError("Metadata input exceeds the 60 KB input limit")
    return model.model_validate_json(raw)


def execute(args):
    if args.metadata_command == "capture":
        from ssdu.metadata_capture_cli import execute as execute_capture

        return execute_capture(args)

    from ssdu.metadata_index import MetadataIndex
    from ssdu.metadata_store import MetadataStore
    from ssdu.metadata_sync_models import MetadataSyncSpec
    from ssdu.retrieval_models import JobBudget
    from ssdu.service import Service

    service = Service(args.store, index_store=False)
    command = args.metadata_command
    if command == "plan":
        spec = _read_json(args.spec, MetadataSyncSpec)
        budget = _read_json(args.budget, JobBudget) if args.budget else None
        return service.plan_metadata_sync(
            spec, offline=args.offline, refresh=args.refresh, budget=budget
        )
    if command == "plan-show":
        return service.get_metadata_plan(args.plan_id)
    if command == "submit":
        return service.submit_metadata_plan(args.plan_id, args.idempotency_key)
    if command == "run":
        return service.run_metadata_job(args.job_id)
    if command == "jobs":
        return service.list_metadata_jobs(args.limit, args.offset)
    if command in {"job", "cancel", "retry"}:
        return getattr(
            service,
            {
                "job": "get_metadata_job",
                "cancel": "cancel_metadata_job",
                "retry": "retry_metadata_job",
            }[command],
        )(args.job_id)
    if command == "snapshots":
        return service.list_metadata_snapshots(args.limit, args.offset)
    if command == "snapshot":
        return service.describe_metadata_snapshot(args.snapshot_id)
    if command == "verify":
        return MetadataStore(args.store).verify(args.snapshot_id)
    if command == "export":
        path = MetadataStore(args.store).export(args.snapshot_id, args.output)
        return {"snapshot_id": args.snapshot_id, "bundle": str(path)}
    if command == "import":
        return {"snapshot_id": MetadataStore(args.store).import_bundle(args.file)}
    if command == "index":
        return MetadataIndex(args.store).import_snapshot(args.snapshot_id)
    if command == "search":
        return service.search_metadata(
            args.query, args.provider, args.limit, args.offset, args.include_retired
        )
    if command == "describe":
        return service.describe_metadata(args.dataset_id, snapshot_id=args.snapshot_id)
    if command == "status":
        return service.metadata_status(args.provider, args.max_age_seconds)
    raise ValueError("Unknown metadata command")
