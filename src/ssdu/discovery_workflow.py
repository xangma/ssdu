"""Apply source-bound metadata updates and compare immutable discovery bundles."""

import copy
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from ssdu.discovery_benchmark import (
    BenchmarkBundle,
    _canonical,
    _references,
    _validate_metadata,
    check_coverage,
    coverage_policy,
    load_bundle,
    run_benchmark,
)
from ssdu.uk_discovery_benchmark import _project


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bundle(value):
    return value if isinstance(value, BenchmarkBundle) else load_bundle(value)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.suffix == ".gz" else raw)


def apply_enrichment(base, capture, output):
    """Derive inputs for a new freeze; old labels, releases and inputs remain pinned.

    A conflicting known coverage fact is retained and reported. This operation does
    not prepare embeddings: explicitly run the local embedding preparation tool.
    """
    base = _bundle(base)
    output = Path(output).resolve()
    if output == base.root or base.root in output.parents or output in base.root.parents:
        raise ValueError("Enrichment output must be separate from the frozen baseline")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Enrichment output must be empty")
    plan = capture["plan"]
    expected = hashlib.sha256((base.root / "manifest.json").read_bytes()).hexdigest()
    if plan.get("benchmark", {}).get("manifest_sha256") != expected:
        raise ValueError("Enrichment was planned against a different baseline manifest")
    if plan.get("selection_split") != "dev":
        raise ValueError("Enrichment target selection must use development questions")
    documents = {doc["id"]: copy.deepcopy(doc) for doc in base.documents}
    sources = {source["id"]: copy.deepcopy(source) for source in base.evidence}
    for source in capture["sources"]:
        if source["id"] in sources and sources[source["id"]] != source:
            raise ValueError("Conflicting enrichment source identity")
        sources[source["id"]] = copy.deepcopy(source)
    changes, conflicts, seen = [], [], set()
    for update in capture["updates"]:
        identifier = update["dataset_id"]
        if identifier in seen or identifier not in documents:
            raise ValueError("Duplicate or unknown enrichment dataset")
        seen.add(identifier)
        doc = documents[identifier]
        if update["base_metadata_sha256"] != _digest(doc):
            raise ValueError("Enrichment dataset differs from its planned baseline")
        if any(doc.get(key) != update[key] for key in ("provider", "native_id")):
            raise ValueError("Enrichment changes dataset identity")
        fields = update["fields"]
        if set(fields) - {"description", "variables", "coverage", "dimensions"}:
            raise ValueError("Unsupported metadata enrichment field")
        if set(fields.get("coverage", {})) - {
            "countries",
            "geography_levels",
            "period_start",
            "period_end",
        }:
            raise ValueError("Enrichment cannot substitute releases, vintages or place coverage")
        refs = update["source_ids"]
        if not refs or set(refs) - sources.keys():
            raise ValueError("Every enrichment update requires retained sources")
        changed = []
        for field, value in fields.items():
            if field == "coverage":
                for key, fact in value.items():
                    if fact is None or fact == []:
                        continue
                    before = doc["coverage"].get(key)
                    if before is not None and before != [] and before != fact:
                        conflicts.append(
                            {
                                "dataset_id": identifier,
                                "field": "coverage." + key,
                                "retained": before,
                                "observed": fact,
                                "references": update["references"],
                            }
                        )
                        continue
                    if before != fact:
                        doc["coverage"][key] = copy.deepcopy(fact)
                        changed.append("coverage." + key)
            elif doc.get(field) != value:
                doc[field] = copy.deepcopy(value)
                changed.append(field)
        provenance = doc.setdefault("provenance", {})
        ids = sorted(set(provenance.get("evidence_ids", [])) | set(refs))
        provenance.update(
            {
                "evidence_ids": ids,
                "enrichment_snapshot_id": capture["snapshot_id"],
                "enrichment_field_provenance": copy.deepcopy(update["field_provenance"]),
            }
        )
        doc["references"] = _references(sources, ids)
        doc["source_urls"] = sorted(
            set(doc.get("source_urls", [])) | {sources[i]["url"] for i in refs}
        )
        doc["provider_revision"] = copy.deepcopy(update.get("provider_revision"))
        doc["dimension_metadata"] = copy.deepcopy(update.get("dimension_metadata", {}))
        doc["enrichment_limitations"] = copy.deepcopy(update.get("limitations", []))
        # The existing projection bounds text, while preserving concepts and coverage.
        documents[identifier] = _project(doc)
        changes.append(
            {"dataset_id": identifier, "fields": changed, "references": update["references"]}
        )
    corpus = {"schema_version": 1, "documents": [documents[i] for i in sorted(documents)]}
    evidence = {"schema_version": 1, "sources": [sources[i] for i in sorted(sources)]}
    _validate_metadata(corpus["documents"], set(sources))
    derivation = json.loads((base.root / "derivation.json").read_bytes())
    derivation["metadata_enrichment"] = {
        "method": "provider-metadata-enrichment-v1",
        "snapshot_id": capture["snapshot_id"],
        "baseline_manifest_sha256": expected,
        "selection_split": "dev",
        "changes": changes,
        "conflicts": conflicts,
        "labels_changed": False,
        "independent_human_judgments": 0,
        "policy": "Provider metadata only; published revision is not a cited historical extract.",
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "corpus.json", corpus)
    write_json(output / "evidence.json", evidence)
    write_json(output / "derivation.json", derivation)
    for name in ("questions.json", "review.json"):
        (output / name).write_bytes((base.root / name).read_bytes())
    return {
        "updated_datasets": len(changes),
        "conflicts": len(conflicts),
        "changes": changes,
        "baseline_manifest_sha256": expected,
        "snapshot_id": capture["snapshot_id"],
        "embedding_preparation_required": True,
    }


def compare_bundles(before, after, *, before_report=None, after_report=None):
    """Paired comparison with fixed questions, splits, labels, engine and model."""
    before, after = _bundle(before), _bundle(after)
    if (before.root / "questions.json").read_bytes() != (
        after.root / "questions.json"
    ).read_bytes():
        raise ValueError("Comparison requires byte-identical questions, labels and splits")
    if {d["id"] for d in before.documents} != {d["id"] for d in after.documents}:
        raise ValueError("Comparison requires the same candidate dataset identities")
    if before.manifest["configuration"] != after.manifest["configuration"]:
        raise ValueError("Comparison requires unchanged search configuration")
    for key in ("id", "revision", "asset_hashes", "dimensions"):
        if before.manifest["model"].get(key) != after.manifest["model"].get(key):
            raise ValueError("Comparison requires the same pinned embedding model")
    if not np.array_equal(before.query_vectors, after.query_vectors):
        raise ValueError("Comparison requires unchanged query vectors")
    reports = [before_report or run_benchmark(before), after_report or run_benchmark(after)]
    for bundle, report in zip((before, after), reports, strict=True):
        if report["benchmark_id"] != _digest(bundle.manifest):
            raise ValueError("Report does not belong to the compared benchmark")
    methods = {}
    for method in ("lexical", "semantic", "hybrid"):
        metrics = {}
        for split in ("dev", "test"):
            old = reports[0]["methods"][method]["by_split"][split]
            new = reports[1]["methods"][method]["by_split"][split]
            metrics[split] = {
                name: {
                    "before": old[name],
                    "after": new[name],
                    "delta": round(new[name]["mean"] - old[name]["mean"], 12)
                    if old[name]["mean"] is not None and new[name]["mean"] is not None
                    else None,
                }
                for name in old
                if name != "metric_denominators"
            }
        old_rows = {r["question_id"]: r for r in reports[0]["methods"][method]["questions"]}
        rows = []
        for row in reports[1]["methods"][method]["questions"]:
            old = old_rows[row["question_id"]]
            rows.append(
                {
                    "question_id": row["question_id"],
                    "split": row["split"],
                    "references": row["references"],
                    "before_top10": [r["id"] for r in old["ranking"]],
                    "after_top10": [r["id"] for r in row["ranking"]],
                    "before_metrics": old["metrics"],
                    "after_metrics": row["metrics"],
                }
            )
        methods[method] = {"by_split": metrics, "questions": rows}
    old_docs = {d["id"]: d for d in before.documents}
    coverage_changes = []
    for doc in after.documents:
        if doc["coverage"] == old_docs[doc["id"]]["coverage"]:
            continue
        for question in before.questions:
            old = check_coverage(
                old_docs[doc["id"]], question["constraints"], policy=coverage_policy(before)
            )
            new = check_coverage(doc, question["constraints"], policy=coverage_policy(after))
            if old != new:
                coverage_changes.append(
                    {
                        "question_id": question["id"],
                        "split": question["split"],
                        "dataset_id": doc["id"],
                        "before": old,
                        "after": new,
                        "references": doc["references"] + question["references"],
                    }
                )
    return {
        "schema_version": 1,
        "before_benchmark_id": reports[0]["benchmark_id"],
        "after_benchmark_id": reports[1]["benchmark_id"],
        "questions_labels_splits_unchanged": True,
        "query_vectors_unchanged": True,
        "configuration_unchanged": True,
        "independent_human_judgments": 0,
        "label_scope": "Existing agent-curated incomplete relevance judgments; no new human labels.",
        "methods": methods,
        "coverage_changes": coverage_changes,
    }
