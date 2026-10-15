"""Explicit provider metadata acquisition and cached inspection."""

from pathlib import Path

from ssdu.models import Request, RetrievalError


def add_arguments(parser):
    commands = parser.add_subparsers(dest="connector_command", required=True)
    datasets = commands.add_parser("datasets", help="Browse a provider's dataset catalogue")
    datasets.add_argument("provider")
    datasets.add_argument("--query", default="")
    describe = commands.add_parser(
        "describe", help="Inspect dataset metadata and supported selections"
    )
    describe.add_argument("provider")
    describe.add_argument("dataset_id")
    values = commands.add_parser("values", help="Browse dimension codes before choosing data")
    values.add_argument("provider")
    values.add_argument("dataset_id")
    values.add_argument("dimension")
    values.add_argument("--search", default="")
    plan = commands.add_parser("plan", help="Resolve a request against provider metadata")
    plan.add_argument("request", type=Path, help="Dataset request JSON file")
    for command in (datasets, values):
        command.add_argument("--limit", type=int, default=20, help="Page size, from 1 to 100")
        command.add_argument("--offset", type=int, default=0, help="Page offset, up to 100000")
    for command in (datasets, describe, values, plan):
        policy = command.add_mutually_exclusive_group()
        policy.add_argument("--offline", action="store_true", help="Use cached metadata; no HTTP")
        policy.add_argument("--refresh", action="store_true", help="Refresh provider metadata")


def execute(args):
    from ssdu.connectors import Connector

    policy = {"offline": args.offline, "refresh": args.refresh}
    try:
        if args.connector_command == "plan":
            with args.request.open("rb") as stream:
                body = stream.read(64_001)
            if len(body) > 64_000:
                raise ValueError("Connector request exceeds the 64 KB input limit")
            import json

            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("Connector request must be a JSON object")
            request = Request(**value)
            return Connector(request.provider, root=args.store).plan_fetch(request, **policy)
        connector = Connector(args.provider, root=args.store)
        if args.connector_command == "datasets":
            return connector.list_datasets(
                args.query, limit=args.limit, offset=args.offset, **policy
            )
        if args.connector_command == "describe":
            return connector.describe_dataset(args.dataset_id, **policy)
        if args.connector_command == "values":
            return connector.list_dimension_values(
                args.dataset_id,
                args.dimension,
                search=args.search,
                limit=args.limit,
                offset=args.offset,
                **policy,
            )
    except KeyError as exc:
        raise RetrievalError("Provider metadata or dimension not found in the local cache") from exc
    raise ValueError("Unknown connector command")
