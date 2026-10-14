"""Import a synthetic table, pin a query, and transfer it without provider access."""

import argparse
import json
import socket
from pathlib import Path


def run(output):
    # This example proves its offline behaviour even if a dependency tries networking.
    def denied(*args, **kwargs):
        raise RuntimeError("The quickstart permits no network access")

    for name in ("getaddrinfo", "create_connection"):
        setattr(socket, name, denied)
    for name in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, name, denied)

    from ssdu.bundles import export_bundle, import_bundle
    from ssdu.collections import Collections
    from ssdu.local_import import import_local
    from ssdu.store import Store

    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose an absent or empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    source_file = output / "notes.csv"
    source_file.write_text("code,value\n001,4\n002,7\n", encoding="utf-8")
    metadata = {
        "dataset_id": "quickstart-notes",
        "title": "Synthetic research notes",
        "description": "Two invented observations for learning SSDU",
        "licence": "CC0-1.0",
        "citation": "SSDU synthetic quickstart fixture",
        "column_types": {"code": "string", "value": "int64"},
        "columns": {"value": {"definition": "Invented measurement", "unit": "count"}},
    }
    source = Store(output / "source")
    snapshot = import_local(source, source_file, metadata)
    collections = Collections(source)
    lock = collections.resolve(
        {
            "name": "notes",
            "entries": {
                "observations": {
                    "snapshot_id": snapshot["product_id"],
                    "query": {
                        "columns": ["code", "value"],
                        "filters": [{"column": "value", "op": "gte", "value": 5}],
                    },
                }
            },
        },
        offline=True,
    )
    original = collections.replay(lock.lock_id)
    bundle = output / "notes.ssdu.zip"
    export_bundle(source, lock, bundle)
    source_file.unlink()

    fresh = Store(output / "fresh")
    imported = import_bundle(fresh, bundle)
    replayed = Collections(fresh).replay("notes")
    assert imported["lock_id"] == lock.lock_id
    assert fresh.verify(snapshot["product_id"]) == source.verify(snapshot["product_id"])
    assert Collections(fresh).get("notes").model_dump() == lock.model_dump()
    assert replayed == original
    rows = replayed["entries"]["observations"]["rows"]
    assert rows == [{"code": "002", "value": 7}]
    return {
        "status": "passed",
        "snapshot_id": snapshot["product_id"],
        "lock_id": lock.lock_id,
        "rows": rows,
        "original_input_removed": not source_file.exists(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output), indent=2))


if __name__ == "__main__":
    main()
