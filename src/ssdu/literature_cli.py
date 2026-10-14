"""Standalone literature operations share the same verified library interfaces."""

import hashlib
import json
from pathlib import Path


def add_arguments(parser):
    commands = parser.add_subparsers(dest="literature_command", required=True)
    capture = commands.add_parser(
        "capture", help="Acquire a bounded pinned plan; cached by default"
    )
    capture.add_argument("plan", type=Path)
    capture.add_argument("--seed-document", type=Path)
    policy = capture.add_mutually_exclusive_group()
    policy.add_argument("--offline", action="store_true")
    policy.add_argument("--refresh", action="store_true")
    commands.add_parser("list", help="List immutable evidence snapshots")
    show = commands.add_parser("show", help="Read a verified evidence snapshot")
    show.add_argument("snapshot_id")
    verify = commands.add_parser("verify", help="Verify evidence and complete raw closure")
    verify.add_argument("snapshot_id")
    search = commands.add_parser("search", help="Search retained papers and data links offline")
    search.add_argument("snapshot_id")
    search.add_argument("--query", default="")
    search.add_argument("--dataset-id")
    search.add_argument("--relationship-type")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    export = commands.add_parser("export", help="Export verified evidence and raw source closure")
    export.add_argument("snapshot_id")
    export.add_argument("--output", type=Path, required=True)
    imported = commands.add_parser("import", help="Verify and import a portable evidence archive")
    imported.add_argument("bundle", type=Path)
    validate = commands.add_parser("validate-links", help="Compile all UK link reviews offline")
    validate.add_argument("snapshot_id")
    validate.add_argument("--provider-snapshot", required=True)
    validate.add_argument("--review", type=Path, action="append", required=True)
    validate.add_argument("--benchmark-base", type=Path)
    revalidate = commands.add_parser(
        "revalidate-links", help="Replay portable link evidence offline"
    )
    revalidate.add_argument("snapshot_id")
    derive_uk = commands.add_parser(
        "derive-uk", help="Derive a UK benchmark from portable evidence"
    )
    derive_uk.add_argument("snapshot_id")
    derive_uk.add_argument("--output", type=Path, required=True)
    derive = commands.add_parser(
        "derive", help="Create referenced draft benchmark inputs and review pack"
    )
    derive.add_argument("snapshot_id")
    derive.add_argument("--output", type=Path, required=True)
    derive.add_argument("--curation", type=Path)
    derive.add_argument(
        "--review", type=Path, help="Explicit named human judgments; pending rows ignored"
    )


def _read(path):
    from ssdu.literature_store import MAX_DOCUMENT_BYTES

    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Literature input exceeds size budget")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Expected a literature JSON object")
    return value, raw


def execute(args):
    from ssdu.literature_store import LiteratureStore

    store = LiteratureStore(args.store)
    command = args.literature_command
    if command == "capture":
        from ssdu.literature_ingest import capture_literature

        plan, _ = _read(args.plan)
        seed, resources = None, None
        if args.seed_document:
            seed, raw = _read(args.seed_document)
            resources = {"uk-seed.json": raw}
        return capture_literature(
            args.store,
            plan,
            seed_document=seed,
            seed_resources=resources,
            offline=args.offline,
            refresh=args.refresh,
        )
    if command == "list":
        return store.list()
    if command == "show":
        return store.read(args.snapshot_id)
    if command == "verify":
        return {"verified": True, **store.verify(args.snapshot_id)}
    if command == "search":
        return store.search(
            args.snapshot_id,
            args.query,
            dataset_id=args.dataset_id,
            relationship_type=args.relationship_type,
            limit=args.limit,
            offset=args.offset,
        )
    if command == "export":
        result = store.export(args.snapshot_id, args.output)
        return {**result, "bundle": result["path"]}
    if command == "import":
        snapshot_id = store.import_bundle(args.bundle)
        return {"snapshot_id": snapshot_id, "counts": store.verify(snapshot_id)["counts"]}
    if command == "validate-links":
        from ssdu.uk_validation_store import publish_validation

        return publish_validation(
            store,
            args.snapshot_id,
            args.provider_snapshot,
            args.review,
            benchmark_base=args.benchmark_base,
        )
    if command == "revalidate-links":
        from ssdu.uk_validation_store import replay_validation

        return replay_validation(store, args.snapshot_id)
    if command == "derive-uk":
        from ssdu.literature_benchmark import write_inputs
        from ssdu.uk_validation_store import derive_validation_benchmark

        derived = derive_validation_benchmark(store, args.snapshot_id)
        output = write_inputs(args.output, derived)
        return {
            "snapshot_id": args.snapshot_id,
            "output": str(output),
            "documents": len(derived["corpus"]["documents"]),
            "questions": len(derived["questions"]["questions"]),
        }
    if command == "derive":
        from ssdu.literature_benchmark import derive_benchmark, import_review, write_inputs

        document = store.read(args.snapshot_id)
        curation = _read(args.curation)[0] if args.curation else None
        if args.review:
            curation = import_review(curation, _read(args.review)[0])
        derived = derive_benchmark(document, snapshot_id=args.snapshot_id, curation=curation)
        output = write_inputs(args.output, derived)
        files = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.iterdir()
            if path.is_file()
        }
        return {
            "snapshot_id": args.snapshot_id,
            "output": str(output),
            "files": files,
            "documents": len(derived["corpus"]["documents"]),
            "questions": len(derived["questions"]["questions"]),
            "human_review_pending": sum(
                not q["suitability_judged"] for q in derived["questions"]["questions"]
            ),
        }
    raise ValueError("Unknown literature command")
