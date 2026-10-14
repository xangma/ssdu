"""Portable, deterministic publication and replay of UK link-review evidence.

Compilation performs no acquisition. The snapshot carries its original frozen
inputs, all review ledgers and their evidence bytes, so it can be revalidated in
a fresh store without the acquisition cache or original source snapshots.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .literature_store import LiteratureError, _canonical, _manifest, _safe_name
from .literature_validation import validate_link_reviews


def _rewrite(value, paths):
    if isinstance(value, dict):
        return {key: _rewrite(item, paths) for key, item in value.items() if key != "local_path"}
    if isinstance(value, list):
        return [_rewrite(item, paths) for item in value]
    if isinstance(value, str):
        head, separator, tail = value.partition("#")
        if head in paths:
            return paths[head] + (separator + tail if separator else "")
    return value


def _snapshot_inputs(store, identifier, prefix):
    manifest = store.verify(identifier)
    root = store.snapshots / identifier
    mapping = {
        path.removeprefix("resources/"): prefix + "/" + path.removeprefix("resources/")
        for path in manifest["files"]
        if path.startswith("resources/")
    }
    resources = {
        target: (root / "resources" / original).read_bytes() for original, target in mapping.items()
    }
    return _rewrite(store.read(identifier), mapping), resources, mapping, manifest


def prepare_inputs(
    store, source_snapshot_id, provider_snapshot_id, review_paths, *, benchmark_base=None
):
    """Verify local inputs and namespace resources before any source merging."""
    source, resources, source_paths, source_manifest = _snapshot_inputs(
        store, source_snapshot_id, "uk"
    )
    provider, provider_resources, provider_paths, provider_manifest = _snapshot_inputs(
        store, provider_snapshot_id, "providers"
    )
    resources.update(provider_resources)
    fallback = {**source_paths, **provider_paths}
    reviews, names = [], set()
    for path in sorted(map(Path, review_paths)):
        path = path.resolve()
        if path.stem in names:
            raise LiteratureError("Review filenames must have distinct stems")
        names.add(path.stem)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise LiteratureError("Review ledger exceeds size budget")
        review = json.loads(path.read_bytes())
        mapping = dict(fallback)
        for resource in review.get("resources", []):
            name = _safe_name(resource["path"])
            local = Path(resource["local_path"]).resolve()
            if not local.is_relative_to(path.parent):
                raise LiteratureError("Review resource escapes the review directory")
            if local.stat().st_size > 64 * 1024 * 1024:
                raise LiteratureError("Review resource exceeds size budget")
            raw = local.read_bytes()
            if hashlib.sha256(raw).hexdigest() != resource["sha256"]:
                raise LiteratureError("Review resource hash mismatch")
            # Shared official captures occur in more than one ledger. Bind them
            # to one content identity; distinct same-named audit files cannot collide.
            target = f"reviews/{resource['sha256']}/{name}"
            if target in resources and resources[target] != raw:
                raise LiteratureError("Conflicting review resource")
            resources[target] = raw
            mapping[name] = target
        reviews.append(_rewrite(review, mapping))
    if benchmark_base is not None:
        base = Path(benchmark_base)
        manifest_raw = (base / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        resources["benchmark-base/manifest.json"] = manifest_raw
        for name in ("corpus.json", "questions.json", "evidence.json"):
            raw = (base / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:
                raise LiteratureError("Base benchmark input hash mismatch")
            resources[f"benchmark-base/{name}"] = raw
    return {
        "source_document": source,
        "provider_document": provider,
        "reviews": reviews,
        "resources": resources,
        "source_snapshot_id": source_snapshot_id,
        "provider_snapshot_id": provider_snapshot_id,
        "original_manifests": {"uk": source_manifest, "providers": provider_manifest},
    }


def _document(inputs, report):
    source = inputs["source_document"]
    decisions = {row["relationship_id"]: row for row in report["records"]}
    papers = {row["paper_id"] for row in report["records"]}
    relationships = []
    datasets = {row["id"]: copy.deepcopy(row) for row in source["datasets"]}
    catalogue = {row["id"]: row for row in inputs["provider_document"]["datasets"]}

    def relationship_proof(proof, rationale):
        return {
            "source_id": proof["source_id"],
            "url": proof["url"],
            "location": (
                proof["location"]
                if isinstance(proof["location"], str)
                else _canonical(proof["location"]).decode()
            ),
            "summary": rationale,
        }

    for original in source["relationships"]:
        row = decisions[original["id"]]
        if row["use_decision"] == "rejected":
            continue
        relation = copy.deepcopy(original)
        relation.update(
            relationship_type={"supported_use": "verified_use", "citation_only": "citation"}.get(
                row["use_decision"], "inferred_mention"
            ),
            review={
                "status": "source_reviewed",
                "reviewer": "SSDU agent evidence review",
                "reviewer_type": "agent",
                "method": "agent",
                "independently_human_validated": False,
            },
            references=row["references"],
            use_decision=row["use_decision"],
            review_basis=row["review_basis"],
            resolution_ids=[r["id"] for r in row["resolutions"]],
        )
        proof = row["evidence"][0]
        relation["evidence"] = relationship_proof(proof, row["rationale"])
        relationships.append(relation)
        if row["use_decision"] != "supported_use":
            continue
        for resolution in row["resolutions"]:
            if resolution["identity_level"] == "family":
                continue
            identifier = resolution["dataset_id"]
            if identifier:
                datasets.setdefault(identifier, copy.deepcopy(catalogue[identifier]))
            else:
                identity = [row["family_id"], resolution["release"], resolution["title"]]
                identifier = "reviewed-release:" + hashlib.sha256(_canonical(identity)).hexdigest()
                official = sorted({proof["source_id"] for proof in resolution["provider_evidence"]})
                refs = [ref for ref in resolution["references"] if ref["id"] in official]
                countries = {
                    "uk-family:english-deprivation": ["England"],
                    "uk-family:welsh-deprivation": ["Wales"],
                }.get(row["family_id"], [])
                datasets.setdefault(
                    identifier,
                    {
                        "id": identifier,
                        "provider": resolution["provider"],
                        "title": resolution["title"],
                        "description": "Named historical edition; exact numerical asset and revision remain unresolved.",
                        "doi": None,
                        "version": resolution["release"],
                        "native_id": None,
                        "coverage": {"countries": countries, "release": resolution["release"]},
                        "source_ids": official,
                        "references": refs,
                        "identification_precision": "named_release",
                        "retrieval_supported": False,
                    },
                )
            resolved_link = copy.deepcopy(relation)
            resolved_link.update(
                id="resolved-use:"
                + hashlib.sha256(_canonical([original["id"], resolution["id"]])).hexdigest(),
                dataset_id=identifier,
                dataset_version=resolution["release"],
                resolution_ids=[resolution["id"]],
                asserted_by="SSDU agent inspection of retained publication and provider evidence",
                references=resolution["references"],
                evidence=relationship_proof(
                    resolution["publication_evidence"][0], resolution["rationale"]
                ),
            )
            relationships.append(resolved_link)
    return {
        "schema_version": 1,
        "sources": report["sources"],
        "papers": [row for row in source["papers"] if row["id"] in papers],
        "datasets": list(datasets.values()),
        "relationships": relationships,
        "validation_report": report,
        "validation_inputs": {
            "source_snapshot_id": inputs["source_snapshot_id"],
            "provider_snapshot_id": inputs["provider_snapshot_id"],
            "descriptor_resource": "compile/inputs.json",
        },
        "limitations": [
            "Verified use denotes agent inspection of retained publication evidence, not independent human review.",
            "Historical release IDs are local evidence identities; an absent provider native ID remains null.",
            "Named editions do not authenticate the exact numerical files or revisions used by authors.",
        ],
    }


def compile_validation(inputs):
    """Recheck the entire candidate ledger and return an immutable publication."""
    resources = dict(inputs["resources"])
    for name, prefix, identifier in (
        ("source_document", "uk", inputs["source_snapshot_id"]),
        ("provider_document", "providers", inputs["provider_snapshot_id"]),
    ):
        manifest = inputs["original_manifests"][prefix]
        _manifest(_canonical(manifest), identifier)
        reverse = {
            prefix + "/" + path.removeprefix("resources/"): path.removeprefix("resources/")
            for path in manifest["files"]
            if path.startswith("resources/")
        }
        original = _canonical(_rewrite(inputs[name], reverse)) + b"\n"
        if hashlib.sha256(original).hexdigest() != manifest["files"]["document.json"]["sha256"]:
            raise LiteratureError("Frozen source document differs from its original snapshot")
        for path, expected in manifest["files"].items():
            if path == "document.json":
                continue
            raw = resources[prefix + "/" + path.removeprefix("resources/")]
            if (
                len(raw) != expected["bytes"]
                or hashlib.sha256(raw).hexdigest() != expected["sha256"]
            ):
                raise LiteratureError("Frozen source resource differs from its original snapshot")
    if any(
        review.get("provider_snapshot_id", inputs["provider_snapshot_id"])
        != inputs["provider_snapshot_id"]
        for review in inputs["reviews"]
    ):
        raise LiteratureError("Review provider snapshot mismatch")
    report = validate_link_reviews(
        inputs["source_document"],
        inputs["provider_document"],
        inputs["reviews"],
        source_snapshot_id=inputs["source_snapshot_id"],
        resources=resources,
    )
    report["provider_snapshot_id"] = inputs["provider_snapshot_id"]
    descriptor = {
        key: inputs[key]
        for key in ("source_snapshot_id", "provider_snapshot_id", "original_manifests")
    }
    descriptor["inputs"] = {}
    for name, value in (
        ("source_document", inputs["source_document"]),
        ("provider_document", inputs["provider_document"]),
        ("reviews", inputs["reviews"]),
    ):
        path = f"compile/{name}.json"
        raw = _canonical(value) + b"\n"
        resources[path] = raw
        descriptor["inputs"][name] = {"path": path, "sha256": hashlib.sha256(raw).hexdigest()}
    resources["compile/inputs.json"] = _canonical(descriptor) + b"\n"
    document = _document(inputs, report)
    # Bind replay inputs to a source, in addition to the manifest's byte closure.
    document["sources"].append(
        {
            "id": "uk-link-review:compilation",
            "type": "operator_validation_inputs",
            "title": "Frozen UK paper–dataset validation inputs",
            "url": "urn:ssdu:uk-link-review:compilation",
            "published_at": None,
            "resource_paths": [
                "compile/inputs.json",
                *[entry["path"] for entry in descriptor["inputs"].values()],
            ],
        }
    )
    return document, resources


def publish_validation(
    store, source_snapshot_id, provider_snapshot_id, review_paths, *, benchmark_base=None
):
    inputs = prepare_inputs(
        store, source_snapshot_id, provider_snapshot_id, review_paths, benchmark_base=benchmark_base
    )
    document, resources = compile_validation(inputs)
    identifier = store.publish(document, resources)
    return {"snapshot_id": identifier, "counts": document["validation_report"]["counts"]}


def replay_validation(store, snapshot_id):
    """Compile from portable bytes, then demand the same full snapshot identity."""
    manifest = store.verify(snapshot_id)
    document = store.read(snapshot_id)
    root = store.snapshots / snapshot_id / "resources"
    resources = {
        name.removeprefix("resources/"): (store.snapshots / snapshot_id / name).read_bytes()
        for name in manifest["files"]
        if name.startswith("resources/")
    }
    descriptor = json.loads(
        (root / document["validation_inputs"]["descriptor_resource"]).read_bytes()
    )
    inputs = {
        key: descriptor[key]
        for key in ("source_snapshot_id", "provider_snapshot_id", "original_manifests")
    }
    for name, entry in descriptor["inputs"].items():
        raw = resources[entry["path"]]
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise LiteratureError("Compilation input hash mismatch")
        inputs[name] = json.loads(raw)
    inputs["resources"] = {
        key: raw for key, raw in resources.items() if not key.startswith("compile/")
    }
    rebuilt, rebuilt_resources = compile_validation(inputs)
    if store.publish(rebuilt, rebuilt_resources) != snapshot_id:
        raise LiteratureError("Offline revalidation changed the snapshot identity")
    return {
        "snapshot_id": snapshot_id,
        "identical_snapshot": True,
        "counts": rebuilt["validation_report"]["counts"],
    }


def derive_validation_benchmark(store, snapshot_id):
    """Derive metadata/question inputs using only the verified snapshot closure."""
    from .discovery_benchmark import LEGACY_COVERAGE_POLICY
    from .uk_discovery_benchmark import build_uk_benchmark

    store.verify(snapshot_id)
    document = store.read(snapshot_id)
    root = store.snapshots / snapshot_id / "resources"
    provider = json.loads((root / "compile/provider_document.json").read_bytes())
    base = {
        name: json.loads((root / f"benchmark-base/{name}.json").read_bytes())
        for name in ("corpus", "questions", "evidence")
    }
    manifest_bytes = (root / "benchmark-base/manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    derived = build_uk_benchmark(
        provider,
        document["validation_report"],
        base["corpus"],
        base["questions"],
        base["evidence"],
        validation_snapshot_id=snapshot_id,
        coverage_policy=manifest.get("configuration", {}).get(
            "coverage_policy", LEGACY_COVERAGE_POLICY
        ),
    )
    derived["derivation"]["base_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    return derived
