"""Explicit full-catalogue metadata capture and verified local transfer commands."""

from pathlib import Path


def add_arguments(parser):
    commands = parser.add_subparsers(dest="metadata_capture_command", required=True)
    plan = commands.add_parser("plan", help="Freeze capture intent without provider access")
    plan.add_argument("spec", type=Path, help="CaptureSpec JSON file")
    policy = plan.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--refresh", action="store_true")
    plan.add_argument("--budget", type=Path, help="Whole-job JobBudget JSON file")
    plan.add_argument("--batch-size", type=int, default=10, help="Descriptor batch size, 1–100")
    show = commands.add_parser("plan-show", help="Read a frozen catalogue capture plan")
    show.add_argument("plan_id")
    submit = commands.add_parser("submit", help="Queue a local capture plan")
    submit.add_argument("plan_id")
    submit.add_argument("--idempotency-key", required=True)
    run = commands.add_parser("run", help="Run one due capture worker explicitly")
    run.add_argument("job_id", nargs="?")
    run.add_argument("--max-batches", type=int, help="Stop after this many durable work units")
    for name in ("job", "cancel", "retry"):
        command = commands.add_parser(name, help=f"{name.capitalize()} a capture job")
        command.add_argument("job_id")
    jobs = commands.add_parser("jobs", help="List retained catalogue capture jobs")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--offset", type=int, default=0)
    report = commands.add_parser("report", help="Verify a retained capture and report coverage")
    report.add_argument("capture_id")
    export = commands.add_parser("export", help="Export a verified portable catalogue capture")
    export.add_argument("capture_id")
    export.add_argument("--output", type=Path, required=True)
    bundle_import = commands.add_parser("import", help="Verify and index a local capture bundle")
    bundle_import.add_argument("file", type=Path)


def execute(args):
    from ssdu.metadata_capture_models import CaptureSpec
    from ssdu.metadata_cli import _read_json
    from ssdu.retrieval_models import JobBudget
    from ssdu.service import Service

    service = Service(args.store, index_store=False)
    command = args.metadata_capture_command
    if command == "plan":
        spec = _read_json(args.spec, CaptureSpec)
        budget = _read_json(args.budget, JobBudget) if args.budget else None
        return service.plan_metadata_capture(
            spec,
            offline=args.offline,
            refresh=args.refresh,
            budget=budget,
            batch_size=args.batch_size,
        )
    if command == "plan-show":
        return service.get_metadata_capture_plan(args.plan_id)
    if command == "submit":
        return service.submit_metadata_capture_plan(args.plan_id, args.idempotency_key)
    if command == "run":
        return service.run_metadata_capture_job(args.job_id, max_batches=args.max_batches)
    if command == "jobs":
        return service.list_metadata_capture_jobs(args.limit, args.offset)
    if command in {"job", "cancel", "retry"}:
        return getattr(
            service,
            {
                "job": "get_metadata_capture_job",
                "cancel": "cancel_metadata_capture_job",
                "retry": "retry_metadata_capture_job",
            }[command],
        )(args.job_id)
    if command == "report":
        return service.metadata_capture_report(args.capture_id)
    if command == "export":
        return service.export_metadata_capture(args.capture_id, args.output)
    if command == "import":
        return service.import_metadata_capture(args.file)
    raise ValueError("Unknown metadata capture command")
