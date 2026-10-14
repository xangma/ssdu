"""Build a metadata-only UK discovery experiment from a reviewed evidence snapshot.

This is a pure derivation: no HTTP, model loading, or mutation of existing bundles.
Agent evidence inspection supplies study-use labels, not independent human judgments.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import unquote, urlsplit

from .discovery_benchmark import (
    DEFAULT_COVERAGE_POLICY,
    LEGACY_COVERAGE_POLICY,
    _coverage_policy,
    _validate_metadata,
    _validate_questions,
    check_coverage,
)
from .literature_benchmark import DerivationError, _constraints, _coverage

PROJECTION_POLICY = {
    "version": 1,
    "title_characters": 120,
    "description_characters": 240,
    "variables": {"items": 4, "characters_each": 24},
    "concepts": {
        "items": None,
        "characters_each": None,
        "reason": "Preserve exact identifiers: concepts also supply hard eligibility predicates.",
    },
    "input": "provider metadata and previously curated metadata only",
    "excluded": "publication text, use decisions, question text, labels and review notes",
    "embedding_limit": "Character caps are not a tokenizer guarantee; preparation must reject inputs exceeding the frozen model's 256-token limit, without silent token truncation.",
}
FAMILIES = {
    "uk-family:nomis": ("nomis", "Nomis census and labour market statistics", []),
    "uk-family:statswales": ("statswales", "StatsWales statistical data", []),
    "uk-family:english-deprivation": ("deprivation", "English indices of deprivation", ["England"]),
    "uk-family:welsh-deprivation": (
        "welsh_government",
        "Welsh Index of Multiple Deprivation",
        ["Wales"],
    ),
}


def _provider(value: str) -> str:
    return "welsh_government" if value == "Welsh Government" else value


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _rows(document: dict, key: str) -> dict[str, dict]:
    if not isinstance(document, dict) or not isinstance(document.get(key), list):
        raise DerivationError(f"Missing {key} list")
    result = {}
    for row in document[key]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise DerivationError(f"Invalid {key} identifier")
        if row["id"] in result:
            raise DerivationError(f"Duplicate {key} identifier: {row['id']}")
        result[row["id"]] = row
    return result


def _record_ids(row: dict, sources: dict) -> list[str]:
    refs = row.get("source_ids", row.get("evidence_ids"))
    if refs is None:
        refs = [r.get("id") if isinstance(r, dict) else r for r in row.get("references", [])]
    if not isinstance(refs, list) or not refs or any(r not in sources for r in refs):
        raise DerivationError("Every derived row requires known references")
    return sorted(set(refs))


def _references(sources: dict, ids: list[str]) -> list[dict]:
    return [
        {k: sources[i].get(k) for k in ("id", "type", "title", "url", "published_at")}
        for i in sorted(set(ids))
    ]


def _merge_sources(*documents: dict) -> dict[str, dict]:
    sources = {}
    for document in documents:
        for key, source in _rows(document, "sources").items():
            previous = sources.get(key, {})
            for field in ("url", "title"):
                if previous.get(field) and source.get(field) != previous[field]:
                    raise DerivationError(f"Conflicting source identity: {key}")
            sources[key] = {**copy.deepcopy(previous), **copy.deepcopy(source)}
    for source in sources.values():
        if not all(isinstance(source.get(k), str) and source[k].strip() for k in ("url", "title")):
            raise DerivationError("Source title and URL must be nonempty strings")
    return sources


def _project(row: dict) -> dict:
    row = copy.deepcopy(row)
    truncated = []
    for field in ("title", "description"):
        value = row.get(field, "")
        if not isinstance(value, str):
            raise DerivationError(f"Metadata {field} must be a string")
        cap = PROJECTION_POLICY[field + "_characters"]
        if len(value) > cap:
            truncated.append(field)
        row[field] = value[:cap]
    for field in ("variables", "concepts"):
        values = row.get(field, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise DerivationError(f"Metadata {field} must be a string list")
        policy = PROJECTION_POLICY[field]
        if policy["items"] is None:
            continue
        projected = [v[: policy["characters_each"]] for v in values[: policy["items"]]]
        if projected != values:
            truncated.append(field)
        row[field] = projected
    row["projection"] = {"policy_version": 1, "truncated_fields": truncated}
    return row


def _provider_metadata(
    dataset: dict, descriptor: dict, sources: dict, snapshot_id: str | None
) -> dict:
    provider, native_id = dataset.get("provider"), dataset.get("native_id")
    identifier = f"{provider}:{native_id}"
    if not provider or not native_id or dataset["id"] != identifier:
        raise DerivationError("Provider records require canonical provider:native_id identity")
    if descriptor.get("dataset_id") != identifier:
        raise DerivationError("Catalogue descriptor does not match dataset")
    refs = _record_ids(dataset, sources)
    coverage = copy.deepcopy(dataset.get("coverage", {}))
    if "country" in coverage:
        coverage.setdefault("countries", [coverage.pop("country")])
    if "release_year" in coverage:
        coverage.setdefault("release", coverage.pop("release_year"))
    coverage = _coverage(coverage)
    columns = descriptor.get("columns", [])
    variables = [c.get("label", c.get("name", "")) for c in columns if isinstance(c, dict)]
    return {
        "id": identifier,
        "provider": provider,
        "native_id": native_id,
        "title": dataset["title"],
        "description": dataset.get("description") or "",
        "variables": variables,
        "concepts": copy.deepcopy(dataset.get("concepts", [])),
        "coverage": coverage,
        "access": dataset.get("access") or "unknown",
        "source_urls": sorted(set([sources[r]["url"] for r in refs] + [descriptor["source_url"]])),
        "references": _references(sources, refs),
        "provenance": {
            "kind": "provider_catalogue_projection",
            "snapshot_id": snapshot_id,
            "original_dataset_id": identifier,
            "evidence_ids": refs,
            "unabridged_metadata_sha256": _digest({"dataset": dataset, "descriptor": descriptor}),
            "resource_pointer": copy.deepcopy(descriptor.get("source_metadata", {})),
        },
        "identity": {
            "level": "catalogue_dataset",
            "synthetic": descriptor.get("source_metadata", {}).get(
                "publisher_native_dataset_id_known"
            )
            is False,
            "data_bytes_pinned": False,
        },
    }


def _official_ids(resolution: dict, sources: dict) -> list[str]:
    ids = {e.get("source_id") for e in resolution.get("provider_evidence", [])}
    if None in ids or ids - sources.keys():
        raise DerivationError("Resolution provider evidence refers to an unknown source")
    for identifier in ids:
        kind = sources[identifier].get("type") or ""
        if "publication" in kind or "bibliographic" in kind:
            raise DerivationError("Publication evidence cannot supply searchable metadata")
    return sorted(ids)


def _family_metadata(identifier: str, provider: str, refs: list[str], sources: dict) -> dict:
    if identifier not in FAMILIES or FAMILIES[identifier][0] != provider:
        raise DerivationError(f"Unknown controlled dataset family: {identifier}")
    _, title, countries = FAMILIES[identifier]
    return {
        "id": identifier,
        "provider": provider,
        "native_id": "",
        "title": title,
        "description": "Provider or index family metadata. Specific tables, historical revisions and observation coverage remain unresolved.",
        "variables": [],
        "concepts": [],
        "coverage": _coverage({"countries": countries}),
        "access": "unknown",
        "source_urls": sorted({sources[i]["url"] for i in refs}),
        "references": _references(sources, refs),
        "identity": {
            "level": "family",
            "synthetic": True,
            "publisher_native_id": None,
            "data_bytes_pinned": False,
        },
        "provenance": {"kind": "controlled_metadata_family", "evidence_ids": refs},
    }


def _paper_tokens(question: dict, sources: dict) -> set[str]:
    tokens = set()
    if question.get("paper_id"):
        tokens.add("paper:" + question["paper_id"])
    for identifier in question.get("evidence_ids", []):
        source = sources[identifier]
        if source.get("type") not in {"research", "publication_reference"}:
            continue
        tokens.add("publication-source:" + identifier)
        title = re.sub(r"\W+", " ", source["title"].casefold()).strip()
        if title:
            tokens.add("publication-title:" + title)
        url = urlsplit(source["url"])
        if url.hostname and url.hostname.casefold() in {"doi.org", "dx.doi.org"}:
            tokens.add("doi:" + unquote(url.path).strip("/").casefold())
    return tokens


def _group_splits(questions: list[dict], sources: dict, base_ids: set[str]) -> list[dict]:
    parents = {}

    def find(token):
        parents.setdefault(token, token)
        if parents[token] != token:
            parents[token] = find(parents[token])
        return parents[token]

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    by_question = {}
    for question in questions:
        tokens = _paper_tokens(question, sources)
        if question["id"] in base_ids and question.get("topic_group"):
            tokens.add("base-topic:" + question["topic_group"])
        if not tokens:
            tokens.add("question:" + question["id"])
        tokens = sorted(tokens)
        for token in tokens:
            union(tokens[0], token)
        by_question[question["id"]] = tokens
    components = {}
    for question in questions:
        root = find(by_question[question["id"]][0])
        components.setdefault(root, []).append(question)
    changes = []
    for component in components.values():
        identities = sorted({t for q in component for t in by_question[q["id"]]})
        component_hash = _digest(identities)
        original_splits = {q["split"] for q in component if q["id"] in base_ids}
        split = (
            next(iter(original_splits))
            if len(original_splits) == 1
            else ("dev" if int(component_hash, 16) % 4 == 0 else "test")
        )
        for question in component:
            previous = question.get("split")
            question["topic_group"] = "publication-group:" + component_hash[:24]
            question["split"] = split
            question["split_identity_keys"] = identities
            if previous is not None and previous != split:
                changes.append(
                    {
                        "question_id": question["id"],
                        "from": previous,
                        "to": split,
                        "reason": "Whole connected publication/source component",
                    }
                )
    return sorted(changes, key=lambda row: row["question_id"])


def build_uk_benchmark(
    provider_document: dict,
    validation_report: dict,
    base_corpus: dict,
    base_questions: dict,
    base_evidence: dict,
    *,
    validation_snapshot_id: str,
    coverage_policy: str = DEFAULT_COVERAGE_POLICY,
) -> dict:
    """Return corpus/questions/evidence/derivation and a pending human review pack.

    Only explicit supported-use proposals become questions. Catalogue candidates are
    audit records, never automatically positive labels. Missing coverage stays unknown.
    """
    if not isinstance(validation_snapshot_id, str) or not validation_snapshot_id:
        raise DerivationError("Validation snapshot ID is required")
    _coverage_policy(coverage_policy)
    sources = _merge_sources(provider_document, validation_report, base_evidence)
    datasets = _rows(provider_document, "datasets")
    descriptors = {d["dataset_id"]: d for d in provider_document["catalogue_descriptors"]}
    if len(descriptors) != len(provider_document["catalogue_descriptors"]) or set(
        descriptors
    ) != set(datasets):
        raise DerivationError("Catalogue descriptors must cover each provider dataset exactly once")
    provider_snapshot = validation_report.get("provider_snapshot_id")
    documents = {
        i: _provider_metadata(d, descriptors[i], sources, provider_snapshot)
        for i, d in sorted(datasets.items())
    }
    originals = _rows(base_corpus, "documents")
    base_id_map = {}
    for old_id, original in sorted(originals.items()):
        candidate = f"{original['provider']}:{original['native_id']}"
        identifier = candidate if candidate in documents else old_id
        base_id_map[old_id] = identifier
        refs = sorted(
            set(original.get("provenance", {}).get("evidence_ids", []))
            | set(documents.get(identifier, {}).get("provenance", {}).get("evidence_ids", []))
        )
        if not refs or set(refs) - sources.keys():
            raise DerivationError("Curated metadata must retain known references")
        merged = copy.deepcopy(original)
        merged["id"] = identifier
        merged["source_urls"] = sorted(
            set(merged.get("source_urls", []))
            | set(documents.get(identifier, {}).get("source_urls", []))
        )
        merged["references"] = _references(sources, refs)
        merged["provenance"] = {
            **copy.deepcopy(documents.get(identifier, {}).get("provenance", {})),
            "kind": "curated_metadata_enrichment"
            if identifier in documents
            else "curated_metadata_copy",
            "evidence_ids": refs,
            "original_base_id": old_id,
            "base_metadata_sha256": _digest(original),
        }
        documents[identifier] = merged

    records = validation_report.get("records")
    if not isinstance(records, list):
        raise DerivationError("Validation report requires reviewed records")
    family_refs = {}
    for record in records:
        family_refs.setdefault(record.get("family_id"), set()).update(
            i
            for resolution in record.get("resolutions", [])
            for i in _official_ids(resolution, sources)
        )
    resolution_map, resolution_bindings, proposals = {}, {}, []
    for record in sorted(records, key=lambda r: r["relationship_id"]):
        for resolution in sorted(record.get("resolutions", []), key=lambda r: r["id"]):
            level, provider = (
                resolution.get("identity_level"),
                _provider(resolution.get("provider")),
            )
            identifier = resolution.get("dataset_id")
            refs = _official_ids(resolution, sources)
            if level == "family":
                identifier = identifier or record.get("family_id")
                if identifier == "uk-family:welsh-deprivation":
                    provider = "welsh_government"
                refs = sorted(
                    set(refs)
                    | family_refs.get(identifier, set())
                    | {
                        i
                        for d in datasets.values()
                        if d.get("provider") == provider
                        for i in d.get("source_ids", [])
                    }
                )
                if not refs:
                    raise DerivationError("Controlled family lacks official metadata references")
                metadata = _family_metadata(identifier, provider, refs, sources)
                if identifier in documents:
                    refs = sorted(
                        set(refs) | set(documents[identifier]["provenance"]["evidence_ids"])
                    )
                    metadata = _family_metadata(identifier, provider, refs, sources)
                    metadata["provenance"]["resolution_ids"] = documents[identifier][
                        "provenance"
                    ].get("resolution_ids", [])
                documents[identifier] = metadata
            elif level in {"specific_dataset", "specific_release"}:
                if not refs:
                    raise DerivationError("Specific identity requires official metadata evidence")
                if identifier in documents:
                    if documents[identifier]["provider"] != provider:
                        raise DerivationError("Resolution changes provider identity")
                    if resolution.get("native_id") != documents[identifier]["native_id"]:
                        raise DerivationError("Resolution changes native identity")
                elif level == "specific_release" and resolution.get("native_id") is None:
                    release = resolution.get("release")
                    if type(release) not in {str, int} or not str(release).strip():
                        raise DerivationError(
                            "Historical release requires an explicit release identity"
                        )
                    title = resolution.get("title")
                    if not isinstance(title, str) or not title:
                        raise DerivationError("Historical release requires an official title")
                    family_id = record.get("family_id")
                    controlled_index = family_id in {
                        "uk-family:english-deprivation",
                        "uk-family:welsh-deprivation",
                    }
                    if controlled_index and provider != FAMILIES[family_id][0]:
                        raise DerivationError(
                            "Historical release changes controlled index publisher"
                        )
                    if controlled_index:
                        title = FAMILIES[family_id][1] + " " + str(release)
                    identity = {
                        "provider": provider,
                        "family": family_id if controlled_index else title.casefold(),
                        "release": str(release),
                    }
                    identifier = "resolved-release:" + _digest(identity)[:24]
                    documents.setdefault(
                        identifier,
                        {
                            "id": identifier,
                            "provider": provider,
                            "native_id": "",
                            "title": title,
                            "description": "Named historical index release. Numeric columns, observation span and immutable data files remain unverified.",
                            "variables": [],
                            "concepts": [],
                            "coverage": _coverage(
                                {
                                    "release": release,
                                    "countries": FAMILIES[family_id][2] if controlled_index else [],
                                }
                            ),
                            "access": "unknown",
                            "source_urls": [],
                            "references": [],
                            "identity": {
                                "level": "specific_release",
                                "synthetic": True,
                                "publisher_native_id": None,
                                "data_bytes_pinned": False,
                            },
                            "provenance": {
                                "kind": "official_historical_release_metadata",
                                "evidence_ids": [],
                            },
                        },
                    )
                else:
                    raise DerivationError(
                        "Specific dataset identity is absent from candidate metadata"
                    )
                metadata = documents[identifier]
                refs = sorted(set(refs) | set(metadata["provenance"]["evidence_ids"]))
                metadata["provenance"]["evidence_ids"] = refs
                metadata["references"] = _references(sources, refs)
                metadata["source_urls"] = sorted(
                    set(metadata["source_urls"]) | {sources[i]["url"] for i in refs}
                )
                if level == "specific_release":
                    release = resolution.get("release")
                    current = metadata["coverage"].get("release")
                    if release is None or (current is not None and str(current) != str(release)):
                        raise DerivationError("Conflicting or missing named release identity")
                    metadata["coverage"]["release"] = release
                    metadata.setdefault("identity", {})["named_release"] = release
                    metadata["identity"]["data_bytes_pinned"] = False
            else:
                raise DerivationError("Unsupported resolution identity level")
            if (
                resolution["id"] in resolution_map
                and resolution_map[resolution["id"]] != identifier
            ):
                raise DerivationError("Conflicting resolution identifier")
            resolution_map[resolution["id"]] = identifier
            resolution_bindings[resolution["id"]] = copy.deepcopy(resolution)
            metadata = documents[identifier]
            metadata["provenance"]["validation_snapshot_id"] = validation_snapshot_id
            metadata["provenance"]["resolution_ids"] = sorted(
                set(metadata["provenance"].get("resolution_ids", [])) | {resolution["id"]}
            )
        if record.get("questions") and record.get("use_decision") != "supported_use":
            raise DerivationError("Positive question proposals require supported publication use")
        for proposal in record.get("questions", []):
            proposals.append((record, proposal))

    questions = []
    base = _rows(base_questions, "questions")
    for original in sorted(base.values(), key=lambda r: r["id"]):
        question = copy.deepcopy(original)
        question["relevance"] = {
            base_id_map[i]: grade for i, grade in original["relevance"].items()
        }
        question["required_groups"] = [
            [base_id_map[i] for i in group] for group in original.get("required_groups", [])
        ]
        question["expected_suitable"] = [
            base_id_map[i] for i in original.get("expected_suitable", [])
        ]
        question["evidence_ids"] = sorted(
            set(question["evidence_ids"])
            | {
                source_id
                for identifier, grade in question["relevance"].items()
                if grade > 0
                for source_id in documents[identifier]["provenance"]["evidence_ids"]
            }
        )
        question["references"] = _references(sources, question["evidence_ids"])
        question["provenance"] = {
            **question.get("provenance", {}),
            "original_base_question_id": original["id"],
            "validation_snapshot_id": validation_snapshot_id,
        }
        questions.append(question)
    for record, proposal in sorted(proposals, key=lambda pair: pair[1]["id"]):
        local_ids = proposal.get("resolution_ids", [])
        local = {r["id"] for r in record.get("resolutions", [])}
        if not local_ids or set(local_ids) - local:
            raise DerivationError("Question requires this publication's resolved identities")
        targets = sorted({resolution_map[i] for i in local_ids})
        refs = set(_record_ids(record, sources)) | set(_record_ids(proposal, sources))
        for target in targets:
            refs.update(documents[target]["provenance"]["evidence_ids"])
        refs = sorted(refs)
        constraints = _constraints(proposal.get("constraints", {}))
        judged = proposal.get("suitability_judged", False)
        if type(judged) is not bool:
            raise DerivationError("Suitability judgment must be explicit boolean")
        if judged and (
            not constraints
            or any(
                check_coverage(documents[i], constraints, policy=coverage_policy)["status"]
                != "eligible"
                for i in targets
            )
        ):
            raise DerivationError(
                "Suitability completion requires explicit, fully satisfied metadata constraints"
            )
        questions.append(
            {
                "id": proposal["id"],
                "text": proposal["text"],
                "paper_id": record["paper_id"],
                "origin": "publication_purpose_adapted",
                "constraints": constraints,
                "evidence_ids": refs,
                "references": _references(sources, refs),
                "relevance": {i: 3 for i in targets},
                "required_groups": [[i] for i in targets],
                "expected_suitable": targets if judged else [],
                "suitability_judged": judged,
                "scenario_basis": proposal.get("scenario_basis"),
                "adaptation_notes": proposal.get("adaptation_notes"),
                "question_kind": proposal.get("question_kind", "discovery"),
                "requested_release_years": copy.deepcopy(
                    proposal.get("requested_release_years", [])
                ),
                "constraint_normalization": copy.deepcopy(
                    proposal.get("constraint_normalization", [])
                ),
                "generation": {
                    "method": "agent_evidence_purpose_adaptation",
                    "human_validated": False,
                },
                "judgment_basis": "agent_inspected_supported_study_use",
                "label_evidence_basis": record.get("review_basis"),
                "label_scope": "Known evidenced use; unlabeled candidates use implicit zero for ranking, not verified negatives.",
                "identity_levels": sorted(
                    {resolution_bindings[i]["identity_level"] for i in local_ids}
                ),
                "target_coverage": {
                    i: check_coverage(documents[i], constraints, policy=coverage_policy)
                    for i in targets
                },
                "review": {"status": "pending", "reviewer": None},
                "provenance": {
                    "validation_snapshot_id": validation_snapshot_id,
                    "relationship_id": record["relationship_id"],
                    "resolution_ids": sorted(local_ids),
                },
            }
        )
    if len({q["id"] for q in questions}) != len(questions):
        raise DerivationError("Question identifiers must be unique across base and new proposals")
    split_changes = _group_splits(questions, sources, set(base))
    corpus = {"schema_version": 1, "documents": [_project(documents[i]) for i in sorted(documents)]}
    question_document = {"schema_version": 1, "questions": sorted(questions, key=lambda q: q["id"])}
    evidence = {"schema_version": 1, "sources": [sources[i] for i in sorted(sources)]}
    _validate_metadata(corpus["documents"], set(sources))
    _validate_questions(question_document["questions"], set(documents), set(sources))
    review = {
        "schema_version": 1,
        "snapshot_id": validation_snapshot_id,
        "source_document_sha256": _digest(validation_report),
        "records": [],
    }
    for question in question_document["questions"]:
        review["records"].append(
            {
                "question_id": question["id"],
                "paper_id": question.get("paper_id"),
                "text": question["text"],
                "status": "pending",
                "reviewer": None,
                "suitability_judged": False,
                "constraints": copy.deepcopy(question["constraints"]),
                "relevance": copy.deepcopy(question["relevance"]),
                "references": copy.deepcopy(question["references"]),
                "provenance": copy.deepcopy(question["provenance"]),
            }
        )
    derivation = {
        "schema_version": 1,
        "method": "uk-evidence-discovery-v1",
        "validation_snapshot_id": validation_snapshot_id,
        "provider_snapshot_id": provider_snapshot,
        "validation_report_sha256": _digest(validation_report),
        "projection_policy": copy.deepcopy(PROJECTION_POLICY),
        "base_dataset_id_map": base_id_map,
        "resolution_dataset_id_map": dict(sorted(resolution_map.items())),
        "split_changes": split_changes,
        "base_input_hashes": {
            name: _digest({"schema_version": 1, key: sorted(value[key], key=lambda r: r["id"])})
            for name, key, value in (
                ("corpus", "documents", base_corpus),
                ("questions", "questions", base_questions),
                ("evidence", "sources", base_evidence),
            )
        },
        "counts": {
            "provider_candidates": len(datasets),
            "reviewed_relationships": len(records),
            "reviewed_papers": len({r["paper_id"] for r in records}),
            "documents": len(documents),
            "base_questions": len(base),
            "new_questions": len(proposals),
            "questions": len(questions),
            "new_papers": len({r["paper_id"] for r, _ in proposals}),
            "question_identity_levels": dict(
                sorted(
                    Counter(
                        level for q in questions for level in q.get("identity_levels", [])
                    ).items()
                )
            ),
            "split_changes": len(split_changes),
        },
        "split_method": "Connected publication ID, DOI, normalized title, source and base topic groups. Retain unambiguous base split; otherwise SHA256 group modulo 4 assigns 0 dev, remainder test.",
        "limitations": [
            "Study-use labels and purpose adaptations are agent inspected; independent human review remains pending.",
            "Unlabeled candidates are not verified negatives; ranking uses a closed-world implicit-zero convention.",
            "Dataset families recur across splits; this is not a dataset-family holdout experiment.",
            "A family label does not resolve a table; named releases do not pin immutable numeric data files.",
            "Catalogue and curated identifiers are merged only on exact provider/native-ID identity.",
            "Unknown geography, periods, columns and access remain unknown; question constraints do not enrich metadata.",
        ],
    }
    if coverage_policy != LEGACY_COVERAGE_POLICY:
        derivation["coverage_policy"] = coverage_policy
    result = {
        "corpus": corpus,
        "questions": question_document,
        "evidence": evidence,
        "derivation": derivation,
        "review_pack": review,
    }
    derivation["input_hashes"] = {
        name + ".json": _digest(result[name]) for name in ("corpus", "questions", "evidence")
    }
    return result
