"""Local review, bounded metadata capture and frozen comparison commands."""

import gzip
import io
import json
from pathlib import Path

from ssdu.discovery_benchmark import load_bundle
from ssdu.discovery_workflow import apply_enrichment, compare_bundles, write_json


def add_arguments(parser):
    commands = parser.add_subparsers(dest="discovery_command", required=True)
    for name in ("review", "diagnose", "plan-enrichment"):
        command = commands.add_parser(name)
        command.add_argument("bundle", type=Path)
        command.add_argument("--output", type=Path, required=True)
        if name in {"review", "diagnose"}:
            command.add_argument("--report", type=Path)
        if name == "review":
            command.add_argument("--question-limit", type=int, default=30)
            command.add_argument("--pool-depth", type=int, default=10)
        if name == "plan-enrichment":
            command.add_argument("--diagnostics", type=Path)
            command.add_argument("--dataset-limit", type=int, default=12)
            command.add_argument("--max-requests", type=int, default=40)
            command.add_argument("--max-total-bytes", type=int, default=32 * 1024 * 1024)
    enrich = commands.add_parser("enrich", help="Capture only planned provider metadata")
    enrich.add_argument("plan", type=Path)
    policy = enrich.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--refresh", action="store_true")
    derive = commands.add_parser("derive-enriched")
    derive.add_argument("bundle", type=Path)
    derive.add_argument("snapshot_id")
    derive.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--before-report", type=Path)
    compare.add_argument("--after-report", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    judgments = commands.add_parser("import-judgments")
    judgments.add_argument("queue", type=Path)
    judgments.add_argument("judgments", type=Path)
    judgments.add_argument("--output", type=Path, required=True)


def read_json(path):
    path = Path(path)
    limit = 96 * 1024 * 1024
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("Expected a bounded regular JSON file")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    raw = path.read_bytes()
    if path.suffix == ".gz":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Expanded JSON exceeds size budget")
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    return value


def _outside(output, inputs):
    resolved = output.resolve()
    for path in inputs:
        root = path.resolve()
        if resolved == root or (root.is_dir() and root in resolved.parents):
            raise ValueError("Write results outside immutable input artifacts")


def execute(args):
    from ssdu.discovery_enrichment import (
        build_enrichment_plan,
        capture_enrichment,
        derive_enrichment,
    )
    from ssdu.discovery_review import build_review_queue, diagnose_discovery, import_judgments
    from ssdu.discovery_review_export import export_review_pack

    command = args.discovery_command
    if command in {"review", "diagnose", "plan-enrichment"}:
        _outside(args.output, [args.bundle])
        bundle = load_bundle(args.bundle)
        if command == "review":
            queue = build_review_queue(
                bundle,
                read_json(args.report) if args.report else None,
                question_limit=args.question_limit,
                pool_depth=args.pool_depth,
            )
            return export_review_pack(queue, args.output)
        if command == "diagnose":
            value = diagnose_discovery(bundle, read_json(args.report) if args.report else None)
        else:
            value = build_enrichment_plan(
                bundle,
                read_json(args.diagnostics) if args.diagnostics else None,
                dataset_limit=args.dataset_limit,
                max_requests=args.max_requests,
                max_total_bytes=args.max_total_bytes,
            )
        write_json(args.output, value)
        return {
            "output": str(args.output),
            "counts": value.get("counts"),
            "targets": len(value.get("targets", [])),
            "kind": value.get("kind"),
        }
    if command == "enrich":
        return capture_enrichment(
            args.store, read_json(args.plan), offline=args.offline, refresh=args.refresh
        )
    if command == "derive-enriched":
        _outside(args.output, [args.bundle])
        return apply_enrichment(
            args.bundle, derive_enrichment(args.store, args.snapshot_id), args.output
        )
    if command == "compare":
        _outside(args.output, [args.before, args.after])
        value = compare_bundles(
            args.before,
            args.after,
            before_report=read_json(args.before_report) if args.before_report else None,
            after_report=read_json(args.after_report) if args.after_report else None,
        )
        write_json(args.output, value)
        return {
            "output": str(args.output),
            "coverage_changes": len(value["coverage_changes"]),
            "questions_labels_splits_unchanged": True,
            "independent_human_judgments": 0,
        }
    if command == "import-judgments":
        _outside(args.output, [args.queue, args.judgments])
        value = import_judgments(read_json(args.queue), read_json(args.judgments))
        write_json(args.output, value)
        return {"output": str(args.output), "counts": value.get("counts")}
    raise ValueError("Unknown discovery command")
