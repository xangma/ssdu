"""Full corpus import reuses either verified retained evidence or the GET checkpoints."""

import hashlib

from ssdu.harvest import MetadataClient
from ssdu.literature_ingest import RICH_CONTEXT_REVISION
from ssdu.literature_store import LiteratureStore
from ssdu.models import RetrievalError
from ssdu.store import canonical


def import_rich_context(
    root, *, from_snapshot=None, source_store=None, client=None, offline=False, refresh=False
):
    from ssdu.literature_sources import parse_rich_context_full

    if offline and refresh or from_snapshot and refresh:
        raise ValueError("A pinned/offline import cannot refresh providers")
    if source_store is not None and from_snapshot is None:
        raise ValueError("source_store requires a retained from_snapshot identifier")
    revision = RICH_CONTEXT_REVISION
    url = (
        f"https://raw.githubusercontent.com/Coleridge-Data-For-Impact/rclc/{revision}/corpus.jsonld"
    )
    transport = client or MetadataClient(root, max_requests=6)
    if from_snapshot:
        source = LiteratureStore(source_store or root)
        source.verify(from_snapshot)
        source_doc = source.read(from_snapshot)
        if not any(row.get("id") == f"rich-context:{revision}" for row in source_doc["sources"]):
            raise ValueError("Retained snapshot does not contain the pinned Rich Context source")
        resources = {}
        for name in (
            "rich-context.jsonld",
            "rich-context.jsonld.capture.json",
            "rich-context-LICENSE.txt",
            "rich-context-LICENSE.txt.capture.json",
        ):
            path = source.path(from_snapshot) / "resources" / name
            if path.is_file():
                resources[name] = path.read_bytes()
        if not {"rich-context.jsonld", "rich-context-LICENSE.txt"} <= resources.keys():
            raise ValueError("Retained snapshot lacks the corpus/licence raw closure")
    else:
        graph = transport.get(url, offline=offline, refresh=refresh)
        licence_url = url.rsplit("/", 1)[0] + "/LICENSE"
        licence = transport.get(licence_url, offline=offline, refresh=refresh)
        resources = {
            **graph.resources("rich-context.jsonld"),
            **licence.resources("rich-context-LICENSE.txt"),
        }
    if b"CC0" not in resources["rich-context-LICENSE.txt"]:
        raise RetrievalError("Unexpected Rich Context licence")
    document = parse_rich_context_full(
        resources["rich-context.jsonld"], source_url=url, revision=revision
    )
    corpus = next(s for s in document["sources"] if s["id"] == f"rich-context:{revision}")
    corpus["licence"] = "CC0-1.0 (retained repository LICENSE)"
    corpus["resource_paths"].append("rich-context-LICENSE.txt")
    plan = {"schema_version": 1, "revision": revision, "mode": "full"}
    resources["full-import-plan.json"] = canonical(plan) + b"\n"
    document["harvest_type"] = "rich_context_full"
    document["ingestion"] = {
        "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
        "source_body_sha256": hashlib.sha256(resources["rich-context.jsonld"]).hexdigest(),
        "independent_human_review": False,
    }
    store = LiteratureStore(root)
    snapshot_id = store.publish(document, resources)
    return {
        "snapshot_id": snapshot_id,
        "counts": store.verify(snapshot_id)["counts"],
        "coverage": document["coverage_report"],
        "provider_attempts": transport.attempts,
        "cache_hits": transport.cache_hits,
        "offline": offline,
    }
