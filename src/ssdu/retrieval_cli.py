"""Local retrieval plans, persistent queue controls and foreground workers."""

import argparse
import json
import math
import time


def _poll_seconds(value):
    value = float(value)
    if not math.isfinite(value) or not 0.1 <= value <= 60:
        raise argparse.ArgumentTypeError("Poll interval must be between 0.1 and 60 seconds")
    return value


def _retry_delay(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 3600:
        raise argparse.ArgumentTypeError("Retry delay must be between 0 and 3600 seconds")
    return value


def add_retrieval_arguments(parser):
    from pathlib import Path

    commands = parser.add_subparsers(dest="retrieval_command", required=True)
    plan = commands.add_parser("plan", help="Inspect a collection locally and save a plan")
    plan.add_argument("spec", type=Path, help="Collection JSON or YAML document")
    policy = plan.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true", help="Require retained snapshots")
    policy.add_argument("--refresh", action="store_true", help="Plan explicit provider refreshes")
    plan.add_argument("--max-requests", type=int, default=128)
    plan.add_argument("--max-total-bytes", type=int, default=100_000_000)
    plan.add_argument("--max-attempts", type=int, default=3)
    plan.add_argument("--retry-delay-seconds", type=_retry_delay, default=1.0)
    show = commands.add_parser("plan-show", help="Read a saved retrieval plan")
    show.add_argument("plan_id")
    submit = commands.add_parser("submit", help="Queue a saved plan without provider access")
    submit.add_argument("plan_id")
    submit.add_argument("--idempotency-key", required=True)


def add_jobs_arguments(parser):
    commands = parser.add_subparsers(dest="jobs_command", required=True)
    listing = commands.add_parser("list", help="List retained job state")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    for name in ("show", "cancel", "retry"):
        command = commands.add_parser(name, help=f"{name.capitalize()} a retained job")
        command.add_argument("job_id")
    work = commands.add_parser("work", help="Run one due job; provider access may be required")
    work.add_argument("job_id", nargs="?", help="Work only this job")
    work.add_argument("--watch", action="store_true", help="Keep processing until interrupted")
    work.add_argument("--poll-seconds", type=_poll_seconds, default=1.0)


def execute(args):
    from ssdu.service import Service

    service = Service(args.store, index_store=False)
    if args.command == "retrieval":
        command = args.retrieval_command
        if command == "plan":
            from ssdu.collections import read_document
            from ssdu.retrieval_models import JobBudget

            budget = JobBudget(
                max_requests=args.max_requests,
                max_total_bytes=args.max_total_bytes,
                max_attempts=args.max_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
            )
            return service.plan_retrieval(
                read_document(args.spec),
                offline=args.offline,
                refresh=args.refresh,
                budget=budget,
            )
        if command == "plan-show":
            return service.get_plan(args.plan_id)
        if command == "submit":
            return service.submit_plan(args.plan_id, args.idempotency_key)
    if args.command == "jobs":
        command = args.jobs_command
        if command == "list":
            return service.list_jobs(limit=args.limit, offset=args.offset)
        if command == "show":
            return service.get_job(args.job_id)
        if command == "cancel":
            return service.cancel_job(args.job_id)
        if command == "retry":
            return service.retry_job(args.job_id)
        if command == "work":
            if not args.watch:
                return service.run_job(args.job_id)
            return _watch(service, args.job_id, args.poll_seconds)
    raise ValueError("Unknown retrieval or job command")


def _watch(service, job_id, poll_seconds):
    """Poll persisted state; Ctrl-C leaves running work recoverable on the next run."""
    try:
        while True:
            result = service.run_job(job_id)
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            if job_id and result.get("status") in {"succeeded", "failed", "cancelled"}:
                return
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print(json.dumps({"status": "worker_stopped", "job_id": job_id}), flush=True)
        raise
