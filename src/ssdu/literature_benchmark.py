"""Deterministic silver discovery inputs derived from a paper–dataset evidence snapshot.

Dataset metadata alone supplies searchable documents. Publication relationships supply
silver labels, not independent relevance or scientific-suitability judgments. No network,
model, or frozen benchmark files are read or modified by this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .discovery_benchmark import CONSTRAINT_FIELDS

RELATION_GRADES = {
    "verified_use": 3,
    "source_asserted_use": 2,
    "citation": 1,
    "inferred_mention": 0,
}
REVIEW_STATUSES = {"pending", "source_reviewed", "human_reviewed"}


class DerivationError(ValueError):
    """Evidence or curation cannot safely become benchmark inputs."""


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode()
    except (TypeError, ValueError) as exc:
        raise DerivationError("Input must contain finite JSON values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _identifier(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:24]


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DerivationError(f"{label} must be a nonempty string")
    return value


def _records(document: dict, name: str) -> dict[str, dict]:
    if not isinstance(document.get(name), list):
        raise DerivationError(f"{name} must be a list")
    result = {}
    for record in document[name]:
        if not isinstance(record, dict):
            raise DerivationError(f"Invalid {name} record")
        identifier = _string(record.get("id"), f"{name}.id")
        if identifier in result:
            raise DerivationError(f"Duplicate {name} identifier: {identifier}")
        result[identifier] = record
    return result


def _source_ids(record: dict, sources: dict, label: str) -> list[str]:
    identifiers = record.get("source_ids")
    if identifiers is None:
        refs = record.get("refs", record.get("references", []))
        if not isinstance(refs, list):
            raise DerivationError(f"{label}.refs must be a list")
        identifiers = [ref.get("id") if isinstance(ref, dict) else ref for ref in refs]
    if not isinstance(identifiers, list) or any(
        not isinstance(identifier, str) or identifier not in sources for identifier in identifiers
    ):
        raise DerivationError(f"Unknown or malformed source IDs in {label}")
    if not identifiers:
        raise DerivationError(f"{label} must retain a source reference")
    return sorted(set(identifiers))


def _references(sources: dict, identifiers: list[str]) -> list[dict]:
    return [
        {
            field: sources[identifier].get(field)
            for field in ("id", "type", "title", "url", "published_at")
        }
        for identifier in identifiers
    ]


def _constraints(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - CONSTRAINT_FIELDS:
        raise DerivationError("Unsupported or malformed constraints")
    for field, item in value.items():
        if field in {"period_start", "period_end"}:
            if type(item) is not int:
                raise DerivationError(f"{field} must be an integer")
        elif field in {"release", "geography_vintage"}:
            if type(item) not in {str, int} or (isinstance(item, str) and not item.strip()):
                raise DerivationError(f"{field} must be a string or integer")
        else:
            _string(item, field)
    if value.get("period_start", -float("inf")) > value.get("period_end", float("inf")):
        raise DerivationError("Inverted requested period")
    return copy.deepcopy(value)


def _coverage(value: Any) -> dict:
    if not isinstance(value, dict):
        raise DerivationError("Dataset coverage must be an object")
    result = copy.deepcopy(value)
    for field in ("countries", "places", "geography_levels", "geography_vintages"):
        values = result.setdefault(field, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise DerivationError(f"coverage.{field} must be a list of strings")
    for field in ("period_start", "period_end"):
        item = result.setdefault(field, None)
        if item is not None and type(item) is not int:
            raise DerivationError(f"coverage.{field} must be an integer or null")
    release = result.setdefault("release", None)
    if release is not None and type(release) not in {str, int}:
        raise DerivationError("coverage.release must be a string, integer or null")
    if (
        result["period_start"] is not None
        and result["period_end"] is not None
        and result["period_start"] > result["period_end"]
    ):
        raise DerivationError("Inverted dataset period")
    return result


def _review(value: Any) -> dict:
    if value is None:
        return {"status": "pending", "reviewer": None}
    if not isinstance(value, dict) or value.get("status") not in REVIEW_STATUSES:
        raise DerivationError("Malformed review status")
    reviewer = value.get("reviewer")
    if value["status"] == "human_reviewed":
        _string(reviewer, "Human reviewer")
    elif reviewer is not None and not isinstance(reviewer, str):
        raise DerivationError("Reviewer must be a string or null")
    return copy.deepcopy(value)


def _question_id(paper_id: str, text: str) -> str:
    return _identifier("lq_", paper_id + "\0" + text)


def _curation_questions(curation: dict | None) -> list[dict]:
    if curation is None:
        return []
    if (
        not isinstance(curation, dict)
        or type(curation.get("schema_version", 1)) is not int
        or curation.get("schema_version", 1) != 1
    ):
        raise DerivationError("Unsupported curation schema")
    questions = curation.get("questions", [])
    if not isinstance(questions, list):
        raise DerivationError("Curation questions must be a list")
    result, seen = [], set()
    for question in questions:
        if not isinstance(question, dict):
            raise DerivationError("Curation question must be an object")
        question = copy.deepcopy(question)
        paper_id = _string(question.get("paper_id"), "Curation paper_id")
        text = _string(question.get("text"), "Curation text")
        identifier = question.setdefault("id", _question_id(paper_id, text))
        _string(identifier, "Curation id")
        if identifier in seen:
            raise DerivationError(f"Duplicate curation question: {identifier}")
        seen.add(identifier)
        _constraints(question.get("constraints", {}))
        _review(question.get("review"))
        result.append(question)
    return result


def import_review(curation: dict | None, review_document: dict) -> dict:
    """Merge deliberately completed named-human records into a curation document.

    Pending template records are ignored. Editing labels without explicitly completing
    a named human review cannot promote those labels to independently reviewed status.
    The returned curation must be used with the same snapshot as the template.
    """
    if (
        not isinstance(review_document, dict)
        or type(review_document.get("schema_version")) is not int
        or review_document.get("schema_version") != 1
    ):
        raise DerivationError("Unsupported review schema")
    _string(review_document.get("snapshot_id"), "Review snapshot_id")
    _string(review_document.get("source_document_sha256"), "Review source_document_sha256")
    records = review_document.get("records")
    if not isinstance(records, list):
        raise DerivationError("Review records must be a list")
    base_questions = _curation_questions(curation)
    if curation is not None:
        for field in ("snapshot_id", "source_document_sha256"):
            if curation.get(field, review_document[field]) != review_document[field]:
                raise DerivationError("Review and curation belong to different source evidence")
    questions = {question["id"]: question for question in base_questions}
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise DerivationError("Review record must be an object")
        identifier = _string(record.get("question_id"), "Review question_id")
        if identifier in seen:
            raise DerivationError(f"Duplicate review question: {identifier}")
        seen.add(identifier)
        status = record.get("status", "pending")
        if status not in {"pending", "human_reviewed"}:
            raise DerivationError("Review records must be pending or human_reviewed")
        if status == "pending":
            continue
        reviewer = _string(record.get("reviewer"), "Human reviewer")
        paper_id = _string(record.get("paper_id"), "Review paper_id")
        existing = questions.get(identifier, {})
        if existing and existing["paper_id"] != paper_id:
            raise DerivationError("Review changes question paper identity")
        question = copy.deepcopy(existing)
        question.update({"id": identifier, "paper_id": paper_id})
        for field in (
            "text",
            "constraints",
            "relevance",
            "expected_suitable",
            "required_groups",
            "suitability_judged",
            "notes",
        ):
            if field in record:
                question[field] = copy.deepcopy(record[field])
        _string(question.get("text"), "Reviewed question text")
        _constraints(question.get("constraints", {}))
        question["review"] = {"status": "human_reviewed", "reviewer": reviewer}
        # Suitability remains unknown unless the human deliberately asserts completion.
        question.setdefault("suitability_judged", False)
        questions[identifier] = question
    return {
        "schema_version": 1,
        "snapshot_id": review_document["snapshot_id"],
        "source_document_sha256": review_document["source_document_sha256"],
        "questions": [questions[key] for key in sorted(questions)],
    }


def derive_benchmark(document: dict, *, snapshot_id: str, curation: dict | None = None) -> dict:
    """Return corpus/questions/evidence, provenance, and an editable review template.

    Curation uses original evidence dataset IDs for relevance and suitability overrides.
    One deterministic title-adapted question is supplied for every linked paper without
    curated questions. All questions belonging to one paper share its hash-based split.
    """
    _string(snapshot_id, "snapshot_id")
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int:
        raise DerivationError("Evidence must declare schema_version 1")
    if document["schema_version"] != 1:
        raise DerivationError("Unsupported evidence schema")
    source_hash = _digest(document)
    if curation is not None:
        if not isinstance(curation, dict):
            raise DerivationError("Curation must be an object")
        if curation.get("snapshot_id", snapshot_id) != snapshot_id:
            raise DerivationError("Curation belongs to a different snapshot")
        if curation.get("source_document_sha256", source_hash) != source_hash:
            raise DerivationError("Curation belongs to different source evidence")
    sources = _records(document, "sources")
    papers = _records(document, "papers")
    datasets = _records(document, "datasets")
    relationships = _records(document, "relationships")
    for source in sources.values():
        _string(source.get("title"), "Source title")
        _string(source.get("url"), "Source URL")
    dataset_ids = {identifier: _identifier("ld_", identifier) for identifier in datasets}
    if len(set(dataset_ids.values())) != len(dataset_ids):
        raise DerivationError("Dataset identifier collision")
    linked = {}
    for relationship in relationships.values():
        paper_id, dataset_id = relationship.get("paper_id"), relationship.get("dataset_id")
        if paper_id not in papers or dataset_id not in datasets:
            raise DerivationError("Relationship refers to missing paper or dataset")
        if relationship.get("relationship_type") not in RELATION_GRADES:
            raise DerivationError("Unsupported relationship type")
        _review(relationship.get("review"))
        evidence = relationship.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("source_id") not in sources:
            raise DerivationError("Relationship must retain source evidence")
        _string(evidence.get("location"), "Relationship evidence location")
        _string(evidence.get("summary"), "Relationship evidence summary")
        linked.setdefault(paper_id, []).append(relationship)
    if not datasets or not linked:
        raise DerivationError("Derivation needs datasets and at least one linked paper")
    corpus = []
    for original_id, dataset in sorted(datasets.items()):
        source_ids = _source_ids(dataset, sources, original_id)
        provider = dataset.get("provider")
        if provider is None:
            providers = dataset.get("providers", [])
            if not isinstance(providers, list) or any(not isinstance(p, str) for p in providers):
                raise DerivationError("Dataset providers must be a list of strings")
            provider = "; ".join(providers) or "unknown"
        _string(provider, "Dataset provider")
        native_id = dataset.get("native_id")
        if native_id is None:
            native_id = original_id
        _string(native_id, "Dataset native_id")
        title = _string(dataset.get("title"), "Dataset title")
        description = dataset.get("description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise DerivationError("Dataset description must be a string or null")
        access = dataset.get("access")
        if access is None:
            access = "unknown"
        _string(access, "Dataset access")
        fields = {}
        for field in ("variables", "concepts"):
            values = dataset.get(field, [])
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise DerivationError(f"Dataset {field} must be a list of strings")
            fields[field] = copy.deepcopy(values)
        corpus.append(
            {
                "id": dataset_ids[original_id],
                "provider": provider,
                "native_id": native_id,
                "title": title,
                "description": description,
                "doi": dataset.get("doi"),
                "version": dataset.get("version"),
                **fields,
                "coverage": _coverage(dataset.get("coverage", {})),
                "access": access,
                "source_urls": [sources[key]["url"] for key in source_ids],
                "provenance": {
                    "kind": "literature_metadata_snapshot",
                    "snapshot_id": snapshot_id,
                    "original_dataset_id": original_id,
                    "evidence_ids": source_ids,
                },
                "references": _references(sources, source_ids),
            }
        )
    curated = _curation_questions(curation)
    by_paper = {}
    for question in curated:
        if question["paper_id"] not in linked:
            raise DerivationError("Curated question refers to an unlinked or missing paper")
        by_paper.setdefault(question["paper_id"], []).append(question)
    questions, review_records = [], []
    for paper_id in sorted(linked):
        paper = papers[paper_id]
        title = _string(paper.get("title"), "Paper title")
        relations = sorted(linked[paper_id], key=lambda row: row["id"])
        source_ids = set(_source_ids(paper, sources, paper_id))
        source_ids.update(relation["evidence"]["source_id"] for relation in relations)
        for relation in relations:
            if any(field in relation for field in ("source_ids", "refs", "references")):
                source_ids.update(_source_ids(relation, sources, relation["id"]))
        source_ids = sorted(source_ids)
        raw_grades = dict.fromkeys(datasets, 0)
        for relation in relations:
            raw_grades[relation["dataset_id"]] = max(
                raw_grades[relation["dataset_id"]], RELATION_GRADES[relation["relationship_type"]]
            )
        linked_use = sorted(
            {
                r["dataset_id"]
                for r in relations
                if r["relationship_type"] in {"verified_use", "source_asserted_use"}
            }
        )
        default_text = f'Which datasets support the study "{title}"?'
        selections = by_paper.get(
            paper_id,
            [
                {
                    "id": _question_id(paper_id, default_text),
                    "paper_id": paper_id,
                    "text": default_text,
                }
            ],
        )
        for selection in sorted(selections, key=lambda row: row["id"]):
            grades = copy.deepcopy(raw_grades)
            overrides = selection.get("relevance", {})
            if not isinstance(overrides, dict) or set(overrides) - datasets.keys():
                raise DerivationError("Relevance overrides require known original dataset IDs")
            if any(type(grade) is not int or grade not in range(4) for grade in overrides.values()):
                raise DerivationError("Relevance grades must be integers from 0 through 3")
            grades.update(overrides)
            suitable = selection.get("expected_suitable", [])
            groups = selection.get("required_groups", [[key] for key in linked_use])
            if not isinstance(suitable, list) or any(
                not isinstance(key, str) or key not in datasets or grades[key] == 0
                for key in suitable
            ):
                raise DerivationError("Suitable IDs must be known, positively graded datasets")
            if not isinstance(groups, list) or any(
                not isinstance(group, list)
                or not group
                or any(not isinstance(key, str) or key not in datasets for key in group)
                for group in groups
            ):
                raise DerivationError("Required groups need known original dataset IDs")
            review = _review(selection.get("review"))
            is_human = review["status"] == "human_reviewed"
            suitability_judged = selection.get("suitability_judged", False)
            if type(suitability_judged) is not bool or (
                suitability_judged and (not is_human or "expected_suitable" not in selection)
            ):
                raise DerivationError("Suitability completion requires a named human review")
            identifier = selection["id"]
            topic = _identifier("paper_", paper_id)
            split = (
                "dev" if int(hashlib.sha256(paper_id.encode()).hexdigest(), 16) % 5 == 0 else "test"
            )
            question = {
                "id": identifier,
                "text": selection["text"],
                "paper_id": paper_id,
                "origin": "literature_curated"
                if paper_id in by_paper
                else "literature_title_adapted",
                "topic_group": topic,
                "split": split,
                "constraints": _constraints(selection.get("constraints", {})),
                "evidence_ids": source_ids,
                "references": _references(sources, source_ids),
                "relevance": {dataset_ids[key]: grades[key] for key in sorted(grades)},
                "expected_suitable": [dataset_ids[key] for key in suitable],
                "suitability_judged": suitability_judged,
                "required_groups": [[dataset_ids[key] for key in group] for group in groups],
                "generation": {
                    "method": "agent_curated" if paper_id in by_paper else "title_adapted",
                    "human_validated": is_human,
                },
                "judgment_basis": "human_reviewed"
                if is_human and "relevance" in selection
                else "silver_usage_relations",
                "collection_basis": "usage_relation_labels",
                "review": review,
                "notes": selection.get(
                    "notes",
                    "Publication titles may repeat dataset names; these are not independent user queries. Link labels do not establish scientific suitability or completeness.",
                ),
                "provenance": {
                    "snapshot_id": snapshot_id,
                    "relationship_ids": [r["id"] for r in relations],
                },
            }
            questions.append(question)
            review_records.append(
                {
                    "question_id": identifier,
                    "paper_id": paper_id,
                    "paper_title": title,
                    "paper": copy.deepcopy(paper),
                    "text": selection["text"],
                    "constraints": question["constraints"],
                    "relevance": grades,
                    "expected_suitable": suitable,
                    "required_groups": groups,
                    "suitability_judged": False,
                    "status": "pending",
                    "reviewer": None,
                    "notes": "Complete independently; set suitability_judged true only after evaluating suitability, including a deliberately empty answer.",
                    "references": question["references"],
                    "relationships": [copy.deepcopy(row) for row in relations],
                }
            )
    identifiers = [question["id"] for question in questions]
    if len(set(identifiers)) != len(identifiers):
        raise DerivationError("Derived question identifier collision")
    evidence = {
        "schema_version": 1,
        "sources": [copy.deepcopy(sources[key]) for key in sorted(sources)],
    }
    result = {
        "corpus": {"schema_version": 1, "documents": corpus},
        "questions": {"schema_version": 1, "questions": questions},
        "evidence": evidence,
        "review_pack": {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "source_document_sha256": source_hash,
            "dataset_id_map": dataset_ids,
            "records": review_records,
        },
    }
    result["derivation"] = {
        "schema_version": 1,
        "method": "literature-silver-v1",
        "snapshot_id": snapshot_id,
        "source_document_sha256": source_hash,
        "curation_sha256": _digest(curation),
        "dataset_id_map": dataset_ids,
        "input_hashes": {
            f"{key}.json": _digest(result[key]) for key in ("corpus", "questions", "evidence")
        },
        "split_method": "sha256(paper_id) modulo 5: 0 dev, otherwise test; whole paper groups",
        "limitations": [
            "Relationship labels are silver, not independently assessed relevance or suitability.",
            "Title-adapted queries can contain dataset names and cause lexical title leakage.",
            "An unjudged suitability list is unknown, not a no-answer case.",
            "Paper-group separation does not hold out dataset families or publication duplicates.",
        ],
        "unlinked_paper_ids": sorted(set(papers) - set(linked)),
    }
    return result


def write_inputs(output_dir: str | Path, derived: dict) -> Path:
    """Atomically publish derivation inputs; refuse symlinks and nonempty destinations.

    Embeddings and the experiment manifest are intentionally prepared separately.
    """
    destination = Path(output_dir).absolute()
    if destination.is_symlink() or (
        destination.exists() and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise DerivationError("Output directory must be absent or empty and cannot be a symlink")
    artifacts = {
        "corpus.json": derived["corpus"],
        "questions.json": derived["questions"],
        "evidence.json": derived["evidence"],
        "derivation.json": derived["derivation"],
        "review.json": derived["review_pack"],
    }
    encoded = {name: _json_bytes(value) for name, value in artifacts.items()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".literature-derivation-", dir=destination.parent))
    try:
        for name, data in encoded.items():
            (staging / name).write_bytes(data)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination
