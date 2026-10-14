"""Offline pooled review and diagnostics for a frozen discovery experiment.

Existing grades describe evidenced use, not exhaustive negatives. Review imports
are human declarations about the supplied pool; they never rewrite global qrels.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .discovery_benchmark import (
    BenchmarkBundle,
    BenchmarkError,
    _canonical,
    _references,
    check_coverage,
    coverage_policy,
    run_benchmark,
)

METHODS = ("lexical", "semantic", "hybrid")
IDENTITIES = {"confirmed", "current_compatible", "family", "unresolved"}
SUITABILITIES = {"eligible", "ineligible", "unknown"}
DEFAULT_SELECTION_POLICY = "neutral-v2"
LEGACY_SELECTION_POLICY = "legacy-v1"


class ReviewError(BenchmarkError):
    """Review artifacts are malformed or are bound to another experiment."""


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _binding(bundle: BenchmarkBundle) -> dict:
    try:
        manifest_bytes = (bundle.root / "manifest.json").read_bytes()
        if json.loads(manifest_bytes) != bundle.manifest:
            raise ReviewError("Manifest file changed after the frozen bundle was loaded")
    except (OSError, ValueError) as exc:
        raise ReviewError("Cannot bind the unchanged frozen manifest file") from exc
    return {
        "benchmark_id": _sha(bundle.manifest),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "documents_sha256": _sha(bundle.documents),
        "questions_sha256": _sha(bundle.questions),
        "evidence_sha256": _sha(bundle.evidence),
        "content_hash_encoding": "Documents, questions and evidence use canonical JSON arrays; manifest_sha256 hashes original file bytes; benchmark_id hashes canonical manifest JSON.",
        "manifest": copy.deepcopy(bundle.manifest),
    }


def _report_rows(bundle: BenchmarkBundle, report: dict | None) -> dict:
    report = run_benchmark(bundle) if report is None else report
    if not isinstance(report, dict) or report.get("benchmark_id") != _sha(bundle.manifest):
        raise ReviewError("Report benchmark binding differs from the frozen bundle")
    if report.get("manifest") != bundle.manifest:
        raise ReviewError("Report manifest differs from the frozen bundle")
    if report.get("replay_sha256") != _sha(
        {key: value for key, value in report.items() if key not in {"replay_sha256", "environment"}}
    ):
        raise ReviewError("Report content hash does not match")
    questions = {question["id"]: question for question in bundle.questions}
    documents = {document["id"]: document for document in bundle.documents}
    rows = {}
    for method in METHODS:
        method_report = report.get("methods", {}).get(method, {})
        records = method_report.get("questions")
        if not isinstance(records, list):
            raise ReviewError(f"Missing {method} report questions")
        by_id = {}
        for row in records:
            if not isinstance(row, dict) or row.get("question_id") not in questions:
                raise ReviewError(f"Unknown {method} report question")
            identifier = row["question_id"]
            if identifier in by_id:
                raise ReviewError(f"Duplicate {method} report question: {identifier}")
            question = questions[identifier]
            if any(row.get(key) != question[key] for key in ("text", "split", "origin")):
                raise ReviewError(f"Changed frozen question in report: {identifier}")
            ranking = row.get("ranking")
            if not isinstance(ranking, list):
                raise ReviewError(f"Missing {method} ranking for {identifier}")
            seen = set()
            for entry in ranking:
                if not isinstance(entry, dict) or entry.get("id") not in documents:
                    raise ReviewError(f"Unknown result for {identifier}")
                if entry["id"] in seen:
                    raise ReviewError(f"Duplicate result for {identifier}")
                seen.add(entry["id"])
                coverage = check_coverage(
                    documents[entry["id"]],
                    question.get("constraints", {}),
                    policy=coverage_policy(bundle),
                )
                if any(
                    entry.get(field) != coverage[field] for field in ("status", "checks", "reasons")
                ):
                    raise ReviewError(f"Changed coverage in report: {identifier}/{entry['id']}")
            by_id[identifier] = row
        if set(by_id) != set(questions):
            raise ReviewError(f"Incomplete {method} report questions")
        rows[method] = by_id
    return rows


def _legacy_portsmouth(question: dict) -> bool:
    place = str(question.get("constraints", {}).get("place", "")).casefold()
    return place in {"portsmouth", "e06000044"} or "portsmouth" in question["text"].casefold()


def _targets(question: dict) -> list[str]:
    return sorted(
        identifier for identifier, grade in question.get("relevance", {}).items() if grade > 0
    )


def _selection_policy(policy):
    if policy not in (DEFAULT_SELECTION_POLICY, LEGACY_SELECTION_POLICY):
        raise ReviewError("Unsupported selection_policy")
    return policy


def _strata(question: dict, documents: dict, policy=DEFAULT_SELECTION_POLICY) -> tuple[str, ...]:
    tags = [f"split:{question['split']}", f"origin:{question['origin']}"]
    tags.extend(f"identity:{value}" for value in question.get("identity_levels", ["unspecified"]))
    tags.extend(
        f"provider:{value}"
        for value in sorted({documents[target]["provider"] for target in _targets(question)})
    )
    tags.append(f"suitability_judged:{question.get('suitability_judged', True)}")
    tags.append(f"kind:{question.get('question_kind', 'unspecified')}")
    if question.get("constraints", {}).get("country"):
        tags.append(f"country:{question['constraints']['country']}")
    if policy == LEGACY_SELECTION_POLICY:
        if _legacy_portsmouth(question):
            tags.append("portsmouth")
    elif question.get("constraints", {}).get("place"):
        tags.append(f"place:{question['constraints']['place']}")
    return tuple(sorted(set(tags)))


def _select(
    bundle: BenchmarkBundle, limit: int, policy=DEFAULT_SELECTION_POLICY
) -> list[tuple[dict, list[str]]]:
    """Greedy coverage of strata, then balanced repetition; ties use question IDs."""
    documents = {document["id"]: document for document in bundle.documents}
    remaining = {question["id"]: question for question in bundle.questions}
    strata = {
        identifier: _strata(question, documents, policy)
        for identifier, question in remaining.items()
    }
    counts: Counter = Counter()
    papers: Counter = Counter()
    selected = []
    weights = {"split": 4, "origin": 4, "provider": 3, "identity": 3}
    if policy == LEGACY_SELECTION_POLICY:
        weights["portsmouth"] = 5
    while remaining and len(selected) < limit:
        candidates = list(remaining)
        if (
            policy == LEGACY_SELECTION_POLICY
            and not selected
            and any(_legacy_portsmouth(question) for question in remaining.values())
        ):
            candidates = [
                identifier for identifier in candidates if _legacy_portsmouth(remaining[identifier])
            ]

        def priority(identifier):
            tags = strata[identifier]
            score = sum(weights.get(tag.split(":", 1)[0], 1) for tag in tags if not counts[tag])
            balance = sum(1 / (1 + counts[tag]) for tag in tags) / len(tags)
            paper = remaining[identifier].get("paper_id", identifier)
            return (-score, papers[paper], -balance, identifier)

        identifier = min(candidates, key=priority)
        question = remaining.pop(identifier)
        reasons = [tag for tag in strata[identifier] if not counts[tag]]
        if not reasons:
            reasons = ["balanced_strata_and_publications"]
        selected.append((question, reasons))
        counts.update(strata[identifier])
        papers[question.get("paper_id", identifier)] += 1
    return selected


def _question_context(question: dict, evidence: dict, policy=DEFAULT_SELECTION_POLICY) -> dict:
    context = {
        "id": question["id"],
        "text": question["text"],
        "split": question["split"],
        "origin": question["origin"],
        "constraints": copy.deepcopy(question.get("constraints", {})),
        "references": _references(evidence, question.get("evidence_ids", [])),
    }
    if policy == LEGACY_SELECTION_POLICY:
        context["portsmouth"] = _legacy_portsmouth(question)
    for field in (
        "identity_levels",
        "paper_id",
        "question_kind",
        "scenario_basis",
        "adaptation_notes",
        "generation",
        "topic_group",
    ):
        if field in question:
            context[field] = copy.deepcopy(question[field])
    return context


def _dataset_context(document: dict, evidence: dict) -> dict:
    return {
        "id": document["id"],
        "title": document["title"],
        "provider": document["provider"],
        "native_id": document["native_id"],
        "description": document["description"],
        "variables": copy.deepcopy(document.get("variables", [])),
        "concepts": copy.deepcopy(document.get("concepts", [])),
        "coverage": copy.deepcopy(document.get("coverage", {})),
        "access": document.get("access"),
        "references": _references(evidence, document.get("provenance", {}).get("evidence_ids", [])),
    }


def build_review_queue(
    bundle: BenchmarkBundle,
    report: dict | None = None,
    *,
    question_limit: int = 30,
    pool_depth: int = 10,
    selection_policy: str = DEFAULT_SELECTION_POLICY,
    cohorts: dict[str, list[str]] | None = None,
) -> dict:
    """Build a deterministically stratified, deduplicated, initially unjudged pool."""
    if type(question_limit) is not int or question_limit < 1:
        raise ReviewError("question_limit must be a positive integer")
    if type(pool_depth) is not int or not 1 <= pool_depth <= max(bundle.cutoffs):
        raise ReviewError("pool_depth exceeds the available frozen ranking cutoff")
    _selection_policy(selection_policy)
    known_questions = {question["id"] for question in bundle.questions}
    if cohorts is not None and (
        selection_policy == LEGACY_SELECTION_POLICY
        or not isinstance(cohorts, dict)
        or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(ids, list)
            or any(
                not isinstance(identifier, str) or identifier not in known_questions
                for identifier in ids
            )
            or len(ids) != len(set(ids))
            for name, ids in cohorts.items()
        )
    ):
        raise ReviewError(
            "Cohorts require named, unique known question IDs under the neutral policy"
        )
    reports = _report_rows(bundle, report)
    documents = {document["id"]: document for document in bundle.documents}
    evidence = {source["id"]: source for source in bundle.evidence}
    questions, rows, pooling = [], [], []
    strata_counts: Counter = Counter()
    for question, selection_reasons in _select(bundle, question_limit, selection_policy):
        question_id = question["id"]
        context = _question_context(question, evidence, selection_policy)
        context["selection_reasons"] = selection_reasons
        questions.append(context)
        strata_counts.update(_strata(question, documents, selection_policy))
        method_ranks: dict[str, dict] = defaultdict(dict)
        for method in METHODS:
            for position, entry in enumerate(
                reports[method][question_id]["ranking"][:pool_depth], 1
            ):
                method_ranks[entry["id"]][method] = {"rank": position, "score": entry["score"]}
        pooled_ids = set(method_ranks)
        pooled_ids.update(_targets(question))
        for identifier in sorted(pooled_ids):
            row_id = "review-pair:" + _sha([question_id, identifier])[:24]
            rows.append(
                {
                    "id": row_id,
                    "question_id": question_id,
                    "dataset_id": identifier,
                    "question_references": context["references"],
                    "dataset_references": _references(
                        evidence,
                        documents[identifier].get("provenance", {}).get("evidence_ids", []),
                    ),
                    "constraints": copy.deepcopy(question.get("constraints", {})),
                    "dataset": _dataset_context(documents[identifier], evidence),
                    "coverage_check": check_coverage(
                        documents[identifier],
                        question.get("constraints", {}),
                        policy=coverage_policy(bundle),
                    ),
                    "review_status": "pending",
                    "human_judgment": None,
                }
            )
            pooling.append(
                {
                    "row_id": row_id,
                    "question_id": question_id,
                    "dataset_id": identifier,
                    "method_ranks": dict(method_ranks.get(identifier, {})),
                    "added_evidenced_target": identifier not in method_ranks,
                    "prior_relevance_grade": question.get("relevance", {}).get(identifier),
                    "prior_suitable": identifier in question.get("expected_suitable", [])
                    if question.get("suitability_judged", True)
                    else None,
                    "prior_label_scope": question.get(
                        "label_scope",
                        "Original curated benchmark labels; independent review pending.",
                    ),
                    "question_references": context["references"],
                    "dataset_references": rows[-1]["dataset_references"],
                }
            )
    queue = {
        "schema_version": 1,
        "artifact_type": "discovery_review_queue",
        "binding": _binding(bundle),
        "selection": {
            "question_limit": question_limit,
            "pool_depth": pool_depth,
            "methods": list(METHODS),
            "policy": "Deterministic stratum coverage, publication balance and question-ID ties.",
        },
        "label_policy": "Pending is unjudged. Evidenced targets supplement the retrieved pool. Unlabelled candidates are not verified negatives. Completed pools do not establish exhaustive global qrels.",
        "counts": {
            "questions": len(questions),
            "pairs": len(rows),
            "human_judgments": 0,
            "pending_pairs": len(rows),
            "strata": dict(sorted(strata_counts.items())),
        },
        "questions": questions,
        "rows": rows,
        "pooling": pooling,
    }
    if selection_policy == LEGACY_SELECTION_POLICY:
        queue["selection"]["policy"] = (
            "Deterministic stratum coverage, publication balance and question-ID ties; Portsmouth included first when present."
        )
        queue["counts"]["portsmouth_questions"] = sum(
            question["portsmouth"] for question in questions
        )
    else:
        queue["selection"]["policy_version"] = 2
        if cohorts:
            selected_ids = {question["id"] for question in questions}
            queue["cohorts"] = {
                name: sorted(set(ids) & selected_ids) for name, ids in sorted(cohorts.items())
            }
    queue["queue_sha256"] = _sha(queue)
    return queue


def diagnose_discovery(
    bundle: BenchmarkBundle,
    report: dict | None = None,
    *,
    selection_policy: str = DEFAULT_SELECTION_POLICY,
) -> dict:
    """Explain misses and metadata gaps without promoting implicit zeros to negatives."""
    _selection_policy(selection_policy)
    reports = _report_rows(bundle, report)
    documents = {document["id"]: document for document in bundle.documents}
    evidence = {source["id"]: source for source in bundle.evidence}
    rows = []
    gaps: dict[str, dict] = {}
    totals = {method: Counter() for method in METHODS}
    unknown_totals = {method: Counter() for method in METHODS}
    for question in sorted(bundle.questions, key=lambda item: item["id"]):
        question_id = question["id"]
        row = _question_context(question, evidence, selection_policy)
        row["label_scope"] = question.get(
            "label_scope", "Original curated labels; not independently reviewed here."
        )
        row["suitability_judged"] = question.get("suitability_judged", True)
        row["independent_review_pending"] = True
        row["methods"] = {}
        for method in METHODS:
            ranking = reports[method][question_id]["ranking"]
            ranked_ids = {entry["id"] for entry in ranking}
            misses = []
            for identifier in _targets(question):
                if identifier not in ranked_ids:
                    misses.append(
                        {
                            "dataset_id": identifier,
                            "prior_relevance_grade": question["relevance"][identifier],
                            "question_references": row["references"],
                            "dataset_references": _references(
                                evidence,
                                documents[identifier].get("provenance", {}).get("evidence_ids", []),
                            ),
                            **check_coverage(
                                documents[identifier],
                                question.get("constraints", {}),
                                policy=coverage_policy(bundle),
                            ),
                        }
                    )
            violations = [
                {
                    "dataset_id": entry["id"],
                    "checks": [
                        check for check in entry["checks"] if check["status"] == "ineligible"
                    ],
                    "question_references": row["references"],
                    "dataset_references": entry["references"],
                }
                for entry in ranking
                if entry["status"] == "ineligible"
            ]
            unknowns = [
                {
                    "dataset_id": entry["id"],
                    "fields": sorted(
                        check["field"] for check in entry["checks"] if check["status"] == "unknown"
                    ),
                    "question_references": row["references"],
                    "dataset_references": entry["references"],
                }
                for entry in ranking
                if any(check["status"] == "unknown" for check in entry["checks"])
            ]
            top_results = [
                {
                    **copy.deepcopy(entry),
                    "question_references": row["references"],
                    "dataset_references": entry["references"],
                }
                for entry in ranking
            ]
            row["methods"][method] = {
                "cutoff": max(bundle.cutoffs),
                "top_results": top_results,
                "evidenced_target_misses": misses,
                "known_violations": violations,
                "unknown_fields": unknowns,
                "unlabelled_result_count": sum(
                    entry["id"] not in question.get("relevance", {}) for entry in ranking
                ),
                "unlabelled_policy": "Unlabelled results have no verified relevance judgment.",
            }
            totals[method].update(
                {
                    "questions_with_evidenced_target_misses": bool(misses),
                    "evidenced_target_misses": len(misses),
                    "known_violation_results": len(violations),
                    "results_with_unknown_fields": len(unknowns),
                }
            )
            for item in unknowns:
                unknown_totals[method].update(item["fields"])
            examined = sorted(ranked_ids | set(_targets(question)))
            for identifier in examined:
                coverage = check_coverage(
                    documents[identifier],
                    question.get("constraints", {}),
                    policy=coverage_policy(bundle),
                )
                fields = sorted(
                    check["field"] for check in coverage["checks"] if check["status"] == "unknown"
                )
                if not fields:
                    continue
                if identifier not in gaps:
                    gaps[identifier] = {
                        "dataset_id": identifier,
                        "provider": documents[identifier]["provider"],
                        "native_id": documents[identifier]["native_id"],
                        "dataset_references": _references(
                            evidence,
                            documents[identifier].get("provenance", {}).get("evidence_ids", []),
                        ),
                        "questions": {},
                    }
                occurrences = gaps[identifier]["questions"]
                if question_id not in occurrences:
                    occurrences[question_id] = {
                        "question_id": question_id,
                        "split": question["split"],
                        "unknown_fields": fields,
                        "methods": [],
                        "evidenced_target": identifier in _targets(question),
                        "references": row["references"],
                    }
                occurrences[question_id]["methods"].append(method)
        rows.append(row)
    metadata_gaps = []
    for identifier in sorted(gaps):
        gap = gaps[identifier]
        gap["questions"] = [gap["questions"][qid] for qid in sorted(gap["questions"])]
        gap["unknown_fields"] = sorted(
            {field for occurrence in gap["questions"] for field in occurrence["unknown_fields"]}
        )
        gap["dev_question_count"] = sum(
            occurrence["split"] == "dev" for occurrence in gap["questions"]
        )
        gap["test_question_count"] = sum(
            occurrence["split"] == "test" for occurrence in gap["questions"]
        )
        metadata_gaps.append(gap)
    diagnostic = {
        "schema_version": 1,
        "artifact_type": "discovery_diagnostics",
        "binding": _binding(bundle),
        "label_policy": "Evidenced-target misses are diagnostic, not exhaustive relevance errors. Unlabelled candidates are not verified negatives. Metadata enrichment selection must use dev occurrences only.",
        "counts": {
            "questions": len(rows),
            "metadata_gap_datasets": len(metadata_gaps),
            "unjudged_suitability_questions": sum(
                not question.get("suitability_judged", True) for question in bundle.questions
            ),
        },
        "methods": {
            method: {
                **dict(totals[method]),
                "unknown_field_counts": dict(sorted(unknown_totals[method].items())),
            }
            for method in METHODS
        },
        "questions": rows,
        "metadata_gaps": metadata_gaps,
    }
    if selection_policy != LEGACY_SELECTION_POLICY:
        diagnostic["policy_version"] = 2
    diagnostic["diagnostics_sha256"] = _sha(diagnostic)
    return diagnostic


def _review_references(value: Any, known: dict) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ReviewError("Every judgment requires references")
    result, seen = [], set()
    for reference in value:
        if isinstance(reference, str):
            if reference not in known:
                raise ReviewError(f"Unknown reference identifier: {reference}")
            reference = known[reference]
        if (
            not isinstance(reference, dict)
            or not isinstance(reference.get("id"), str)
            or not reference["id"].strip()
        ):
            raise ReviewError("References require a nonempty identifier")
        if reference["id"] in seen:
            raise ReviewError("Duplicate judgment reference")
        seen.add(reference["id"])
        if reference["id"] in known:
            if any(
                reference.get(field) != known[reference["id"]].get(field)
                for field in ("title", "url")
                if field in reference
            ):
                raise ReviewError("Changed known citation in judgment")
            reference = known[reference["id"]]
        else:
            url = reference.get("url", "")
            if not isinstance(url, str):
                raise ReviewError("New references require a title and HTTP(S) URL")
            parsed = urlparse(url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.netloc
                or not isinstance(reference.get("title"), str)
                or not reference["title"].strip()
            ):
                raise ReviewError("New references require a title and HTTP(S) URL")
        result.append(copy.deepcopy(reference))
    return result


def import_judgments(queue: dict, judgments: dict) -> dict:
    """Validate human declarations and retain pending pairs separately from qrels."""
    if (
        not isinstance(queue, dict)
        or queue.get("artifact_type") != "discovery_review_queue"
        or queue.get("schema_version") != 1
    ):
        raise ReviewError("Expected a version-1 discovery review queue")
    if queue.get("queue_sha256") != _sha(
        {key: value for key, value in queue.items() if key != "queue_sha256"}
    ):
        raise ReviewError("Review queue content hash does not match")
    if (
        not isinstance(judgments, dict)
        or judgments.get("queue_sha256") != queue["queue_sha256"]
        or judgments.get("benchmark_id") != queue["binding"]["benchmark_id"]
    ):
        raise ReviewError("Judgments have a stale or missing queue/benchmark binding")
    incoming = judgments.get("judgments")
    if not isinstance(incoming, list):
        raise ReviewError("Missing judgments list")
    rows = copy.deepcopy(queue["rows"])
    by_id = {row["id"]: row for row in rows}
    known = {
        reference["id"]: reference
        for row in rows
        for key in ("question_references", "dataset_references")
        for reference in row[key]
    }
    seen = set()
    for judgment in incoming:
        if not isinstance(judgment, dict) or judgment.get("row_id") not in by_id:
            raise ReviewError("Unknown judgment row")
        row_id = judgment["row_id"]
        if row_id in seen:
            raise ReviewError("Duplicate judgment row")
        seen.add(row_id)
        reviewer = judgment.get("reviewer")
        if (
            not isinstance(reviewer, dict)
            or reviewer.get("type") != "human"
            or not isinstance(reviewer.get("name"), str)
            or not reviewer["name"].strip()
            or reviewer.get("automated", False)
            or judgment.get("generated_by")
        ):
            raise ReviewError(
                "Judgments require a declared human reviewer; automated judgments are not independent review"
            )
        if type(judgment.get("relevance")) is not int or judgment["relevance"] not in range(4):
            raise ReviewError("Relevance must be an integer from 0 to 3")
        if (
            not isinstance(judgment.get("identity"), str)
            or judgment["identity"] not in IDENTITIES
            or not isinstance(judgment.get("suitability"), str)
            or judgment["suitability"] not in SUITABILITIES
        ):
            raise ReviewError("Invalid identity or suitability judgment")
        if not isinstance(judgment.get("reason"), str) or not judgment["reason"].strip():
            raise ReviewError("Judgments require a reason")
        try:
            timestamp = datetime.fromisoformat(judgment["reviewed_at"])
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ReviewError("reviewed_at must be an ISO timestamp with a timezone") from exc
        if timestamp.tzinfo is None:
            raise ReviewError("reviewed_at must include a timezone")
        cleaned = {
            key: copy.deepcopy(judgment[key])
            for key in (
                "row_id",
                "reviewer",
                "reviewed_at",
                "reason",
                "relevance",
                "identity",
                "suitability",
            )
        }
        cleaned["references"] = _review_references(judgment.get("references"), known)
        by_id[row_id]["human_judgment"] = cleaned
        by_id[row_id]["review_status"] = "reviewed"
    progress = []
    for question in queue["questions"]:
        question_rows = [row for row in rows if row["question_id"] == question["id"]]
        reviewed = sum(row["review_status"] == "reviewed" for row in question_rows)
        progress.append(
            {
                "question_id": question["id"],
                "reviewed_pairs": reviewed,
                "pending_pairs": len(question_rows) - reviewed,
                "pool_complete": bool(question_rows) and reviewed == len(question_rows),
                "global_qrels_complete": False,
                "references": question["references"],
            }
        )
    result = {
        "schema_version": 1,
        "artifact_type": "discovery_review_judgments",
        "queue_sha256": queue["queue_sha256"],
        "benchmark_id": queue["binding"]["benchmark_id"],
        "reviewer_verification": "Human identity is declared by the importer, not authenticated by this file format.",
        "label_policy": "Only explicitly reviewed pairs are judged. A complete retrieved pool is not exhaustive global qrels. Original benchmark questions and test split remain unchanged.",
        "counts": {
            "pairs": len(rows),
            "human_judgments": len(seen),
            "pending_pairs": len(rows) - len(seen),
            "complete_question_pools": sum(item["pool_complete"] for item in progress),
        },
        "rows": rows,
        "question_progress": progress,
    }
    result["judgments_sha256"] = _sha(result)
    return result
