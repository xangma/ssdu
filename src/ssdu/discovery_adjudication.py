"""Source-backed agent adjudication of a frozen, incomplete discovery pool.

These artifacts never declare human review or rewrite global benchmark labels.
Evidence is checked locally; no retrieval or model runtime is required.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .discovery_review import IDENTITIES, METHODS, SUITABILITIES, ReviewError, _sha


class AdjudicationError(ReviewError):
    """An adjudication is incomplete, stale, or lacks verifiable evidence."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdjudicationError(f"Missing {label}")
    return value


def _grade(value: Any) -> int:
    if type(value) is not int or value not in range(4):
        raise AdjudicationError("Relevance grade must be an integer from 0 to 3")
    return value


def _agent(value: Any) -> None:
    if not isinstance(value, dict) or value.get("type") != "agent":
        raise AdjudicationError("Only explicitly declared agent assessments are accepted")
    _text(value.get("name"), "agent reviewer name")


def _unique(records: Any, key: str, label: str) -> dict:
    if not isinstance(records, list):
        raise AdjudicationError(f"Missing {label} array")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise AdjudicationError(f"Invalid {label} record")
        identifier = _text(record.get(key), f"{label} {key}")
        if identifier in result:
            raise AdjudicationError(f"Duplicate {label}: {identifier}")
        result[identifier] = record
    return result


def _queue(queue: dict) -> tuple[dict, dict, dict]:
    if (
        not isinstance(queue, dict)
        or type(queue.get("schema_version")) is not int
        or queue.get("schema_version") != 1
        or queue.get("artifact_type") != "discovery_review_queue"
    ):
        raise AdjudicationError("Invalid frozen review queue")
    if queue.get("queue_sha256") != _sha(
        {key: value for key, value in queue.items() if key != "queue_sha256"}
    ):
        raise AdjudicationError("Queue content hash differs from its binding")
    binding = queue.get("binding")
    if not isinstance(binding, dict):
        raise AdjudicationError("Invalid benchmark binding")
    benchmark_id = binding.get("benchmark_id")
    if not isinstance(benchmark_id, str) or not re.fullmatch("[0-9a-f]{64}", benchmark_id):
        raise AdjudicationError("Invalid benchmark binding")
    questions = _unique(queue.get("questions"), "id", "question")
    selection = queue.get("selection", {})
    if not isinstance(selection, dict):
        raise AdjudicationError("Invalid review selection policy")
    policy_version = selection.get("policy_version", 1)
    if type(policy_version) is not int or policy_version not in (1, 2):
        raise AdjudicationError("Unsupported review selection policy")
    cohorts = queue.get("cohorts", {})
    if (
        not isinstance(cohorts, dict)
        or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(ids, list)
            or any(
                not isinstance(identifier, str) or identifier not in questions for identifier in ids
            )
            or len(ids) != len(set(ids))
            for name, ids in cohorts.items()
        )
        or (cohorts and policy_version == 1)
    ):
        raise AdjudicationError("Invalid bound review cohorts")
    rows = _unique(queue.get("rows"), "id", "pool row")
    pooling = _unique(queue.get("pooling"), "row_id", "pooling")
    if not questions or not rows or set(rows) != set(pooling):
        raise AdjudicationError("Queue and pooling rows are incomplete")
    pairs = set()
    for identifier, row in rows.items():
        if row.get("question_id") not in questions:
            raise AdjudicationError("Unknown pool question")
        _text(row.get("dataset_id"), "dataset ID")
        pair = (row["question_id"], row["dataset_id"])
        if pair in pairs:
            raise AdjudicationError("Duplicate pooled question/dataset pair")
        pairs.add(pair)
        pooled = pooling[identifier]
        if any(row.get(key) != pooled.get(key) for key in ("question_id", "dataset_id")):
            raise AdjudicationError("Pool identity differs from pooling record")
        prior = pooled.get("prior_relevance_grade")
        if prior is not None:
            _grade(prior)
        if pooled.get("prior_suitable") is not None and type(pooled["prior_suitable"]) is not bool:
            raise AdjudicationError("Invalid prior suitability declaration")
        ranks = pooled.get("method_ranks")
        if not isinstance(ranks, dict) or set(ranks) - set(METHODS):
            raise AdjudicationError("Invalid frozen method ranks")
        for entry in ranks.values():
            if (
                not isinstance(entry, dict)
                or type(entry.get("rank")) is not int
                or entry["rank"] < 1
            ):
                raise AdjudicationError("Invalid frozen method rank")
    for question in questions:
        for method in METHODS:
            ranks = [
                row["method_ranks"][method]["rank"]
                for row in pooling.values()
                if row["question_id"] == question and method in row["method_ranks"]
            ]
            if len(set(ranks)) != len(ranks) or sorted(ranks) != list(range(1, len(ranks) + 1)):
                raise AdjudicationError("Duplicate or missing frozen method rank")
    return questions, rows, pooling


def _bindings(value: dict, queue: dict) -> None:
    if (
        not isinstance(value, dict)
        or value.get("queue_sha256") != queue["queue_sha256"]
        or value.get("benchmark_id") != queue["binding"]["benchmark_id"]
    ):
        raise AdjudicationError("Assessment queue/benchmark binding differs")


class _Evidence:
    def __init__(self, root: Path):
        try:
            self.root = Path(root).resolve(strict=True)
            if not self.root.is_dir():
                raise AdjudicationError("evidence_root must be a directory")
        except OSError as exc:
            raise AdjudicationError("Cannot access evidence_root") from exc
        self.cache: dict[tuple[str, str], bytes] = {}
        self.files: dict[str, str] = {}

    def source(self, path: Any, digest: Any) -> tuple[str, bytes]:
        path = _text(path, "evidence path")
        if (
            "\\" in path
            or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise AdjudicationError("Evidence path must be safe and relative")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise AdjudicationError("Invalid evidence SHA256")
        candidate = self.root
        try:
            for part in PurePosixPath(path).parts:
                candidate /= part
                if candidate.is_symlink():
                    raise AdjudicationError("Symlink evidence is forbidden")
            if not candidate.is_file() or not candidate.resolve().is_relative_to(self.root):
                raise AdjudicationError("Evidence is not a regular file inside evidence_root")
            key = (path, digest)
            if key not in self.cache:
                data = candidate.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise AdjudicationError(f"Evidence hash mismatch: {path}")
                self.cache[key] = data
            if path in self.files and self.files[path] != digest:
                raise AdjudicationError("Conflicting evidence hashes")
            self.files[path] = digest
            return path, self.cache[key]
        except OSError as exc:
            raise AdjudicationError(f"Cannot read evidence: {path}") from exc

    def reference(self, reference: dict) -> dict:
        if not isinstance(reference, dict):
            raise AdjudicationError("Invalid source reference")
        path, data = self.source(reference.get("path"), reference.get("sha256"))
        url = _text(reference.get("url"), "source URL")
        selector = _text(reference.get("selector"), "source selector")
        excerpt = _text(reference.get("excerpt"), "literal source excerpt")
        try:
            literal = excerpt in data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdjudicationError("Literal excerpts require UTF-8 evidence") from exc
        if not literal:
            raise AdjudicationError(f"Excerpt is not literal in source: {path}")
        return {
            "path": path,
            "sha256": reference["sha256"],
            "url": url,
            "selector": selector,
            "excerpt": excerpt,
        }

    def references(self, references: Any) -> list[dict]:
        if not isinstance(references, list) or not references:
            raise AdjudicationError("At least one source reference is required")
        return [self.reference(reference) for reference in references]

    def inherited(self, references: Any) -> tuple[list[dict], list[dict]]:
        if not isinstance(references, list) or not references:
            raise AdjudicationError("Independent assessment has no inspected references")
        normalized, provenance = [], []
        for reference in references:
            if not isinstance(reference, dict) or reference.get("inspected") is not True:
                raise AdjudicationError("Invalid independent inspected reference")
            self.source(reference.get("retained_path"), reference.get("sha256"))
            provenance.append(copy.deepcopy(reference))
            text_source = reference.get("extracted_text", reference)
            path, data = self.source(text_source.get("retained_path"), text_source.get("sha256"))
            url = reference.get("url")
            if not url:
                if reference.get("source_kind") != "blind_candidate_assertion":
                    raise AdjudicationError("Independent source reference has no URL")
                # Identify the local packet without inventing external corroboration.
                url = f"urn:ssdu:source:{reference['sha256']}"
            item = {
                "path": path,
                "sha256": text_source["sha256"],
                "url": _text(url, "source URL"),
                "selector": _text(reference.get("selector"), "source selector"),
                "source_kind": reference.get("source_kind"),
                "inspection": reference.get("inspection"),
                "origin": "independent_assessment",
            }
            excerpt = reference.get("excerpt")
            try:
                literal = (
                    isinstance(excerpt, str) and bool(excerpt) and excerpt in data.decode("utf-8")
                )
            except UnicodeDecodeError:
                literal = False
            if literal:
                item.update(excerpt=excerpt, excerpt_status="literal")
            else:
                item["excerpt_status"] = (
                    "inherited_selection_not_literal" if excerpt else "not_supplied"
                )
            if text_source is not reference:
                item["original_source"] = {
                    "path": reference["retained_path"],
                    "sha256": reference["sha256"],
                }
            normalized.append(item)
        return normalized, provenance


def _assessment(value: dict) -> None:
    _grade(value.get("relevance"))
    if (
        not isinstance(value.get("study_dataset_identity"), str)
        or value["study_dataset_identity"] not in IDENTITIES
    ):
        raise AdjudicationError("Invalid study dataset identity")
    if (
        not isinstance(value.get("analysis_suitability"), str)
        or value["analysis_suitability"] not in SUITABILITIES
    ):
        raise AdjudicationError("Invalid tri-state analysis suitability")
    _text(value.get("reason"), "assessment reason")
    _text(value.get("suitability_reason"), "suitability reason")


def build_adjudication(
    queue: dict, independent: dict, submissions: list[dict], *, evidence_root: Path
) -> dict:
    """Resolve every explicit disagreement without changing the frozen inputs."""
    questions, rows, pooling = _queue(queue)
    if (
        not isinstance(independent, dict)
        or type(independent.get("schema_version")) is not int
        or independent.get("schema_version") != 1
    ):
        raise AdjudicationError("Invalid independent assessment")
    _bindings(independent, queue)
    _agent(independent.get("reviewer"))
    judgments = _unique(independent.get("judgments"), "row_id", "independent judgment")
    if set(judgments) != set(rows):
        raise AdjudicationError("Independent judgments do not cover the exact pool")
    evidence = _Evidence(evidence_root)
    inherited = {}
    required = {}
    for identifier, judgment in judgments.items():
        row = rows[identifier]
        if any(judgment.get(key) != row[key] for key in ("question_id", "dataset_id")):
            raise AdjudicationError("Independent judgment identity differs from pool")
        if "queue_sha256" in judgment or "benchmark_id" in judgment:
            _bindings(judgment, queue)
        if "reviewer" in judgment:
            _agent(judgment["reviewer"])
        _assessment(judgment)
        inherited[identifier] = evidence.inherited(judgment.get("inspected_references"))
        prior = pooling[identifier]
        reasons = []
        if (
            prior.get("prior_relevance_grade") is not None
            and prior["prior_relevance_grade"] != judgment["relevance"]
        ):
            reasons.append("explicit_relevance_disagreement")
        if prior.get("prior_suitable") is True and judgment["analysis_suitability"] != "eligible":
            reasons.append("prior_suitable_not_confirmed")
        if reasons:
            required[identifier] = reasons
    if not isinstance(submissions, list):
        raise AdjudicationError("Submissions must be an array")
    decisions, question_notes, reviewers = {}, {}, {}
    for submission in submissions:
        if (
            not isinstance(submission, dict)
            or type(submission.get("schema_version")) is not int
            or submission.get("schema_version") != 1
        ):
            raise AdjudicationError("Invalid adjudication submission")
        _bindings(submission, queue)
        _agent(submission.get("reviewer"))
        submission_hash = _sha(submission)
        reviewers[submission_hash] = copy.deepcopy(submission["reviewer"])
        for field, additional in (("decisions", False), ("additional_decisions", True)):
            records = submission.get(field, [])
            if not isinstance(records, list):
                raise AdjudicationError(f"Invalid {field} array")
            for decision in records:
                if not isinstance(decision, dict) or decision.get("row_id") not in rows:
                    raise AdjudicationError("Unknown adjudication row")
                identifier = decision["row_id"]
                if identifier in decisions:
                    raise AdjudicationError("Duplicate adjudication decision")
                if (identifier in required) == additional:
                    raise AdjudicationError(
                        "Required and additional decisions must be distinguished"
                    )
                if any(
                    decision.get(key) != rows[identifier][key]
                    for key in ("question_id", "dataset_id")
                ):
                    raise AdjudicationError("Decision identity differs from pool")
                prior = pooling[identifier].get("prior_relevance_grade")
                if decision.get("prior_grade") is not None:
                    _grade(decision["prior_grade"])
                if "prior_grade" not in decision or decision["prior_grade"] != prior:
                    raise AdjudicationError("Decision has stale prior grade")
                if (
                    type(decision.get("independent_grade")) is not int
                    or decision["independent_grade"] != judgments[identifier]["relevance"]
                ):
                    raise AdjudicationError("Decision has stale independent grade")
                _grade(decision.get("final_grade"))
                _text(decision.get("reason"), "decision reason")
                final_suitability = decision.get(
                    "final_suitability", judgments[identifier]["analysis_suitability"]
                )
                if not isinstance(final_suitability, str) or final_suitability not in SUITABILITIES:
                    raise AdjudicationError("Invalid final tri-state suitability")
                if final_suitability != judgments[identifier]["analysis_suitability"]:
                    _text(decision.get("suitability_reason"), "changed suitability reason")
                final_identity = decision.get(
                    "final_identity", judgments[identifier]["study_dataset_identity"]
                )
                if not isinstance(final_identity, str) or final_identity not in IDENTITIES:
                    raise AdjudicationError("Invalid final dataset identity")
                normalized = evidence.references(decision.get("references"))
                decisions[identifier] = {
                    **copy.deepcopy(decision),
                    "references": normalized,
                    "submission_sha256": submission_hash,
                    "reviewer": copy.deepcopy(submission["reviewer"]),
                    "additional": additional,
                }
        notes = submission.get("question_decisions", [])
        if not isinstance(notes, list):
            raise AdjudicationError("Invalid question decisions")
        for note in notes:
            if not isinstance(note, dict) or note.get("question_id") not in questions:
                raise AdjudicationError("Unknown question decision")
            identifier = note["question_id"]
            note_key = (identifier, submission_hash)
            if note_key in question_notes:
                raise AdjudicationError("Duplicate question decision")
            _text(note.get("reason"), "question decision reason")
            question_notes[note_key] = {
                **copy.deepcopy(note),
                "references": evidence.references(note.get("references")),
                "submission_sha256": submission_hash,
            }
    missing = set(required) - set(decisions)
    if missing:
        raise AdjudicationError(f"Unresolved required disagreements: {', '.join(sorted(missing))}")
    final = []
    for identifier in sorted(rows):
        original = judgments[identifier]
        decision = decisions.get(identifier)
        source_refs, provenance = inherited[identifier]
        entry = {
            "row_id": identifier,
            "question_id": rows[identifier]["question_id"],
            "dataset_id": rows[identifier]["dataset_id"],
            "relevance": decision["final_grade"] if decision else original["relevance"],
            "study_dataset_identity": decision.get(
                "final_identity", original["study_dataset_identity"]
            )
            if decision
            else original["study_dataset_identity"],
            "analysis_suitability": decision.get(
                "final_suitability", original["analysis_suitability"]
            )
            if decision
            else original["analysis_suitability"],
            "reason": decision["reason"] if decision else original["reason"],
            "suitability_reason": decision.get("suitability_reason", original["suitability_reason"])
            if decision
            else original["suitability_reason"],
            "identity_reason": decision["reason"]
            if decision and "final_identity" in decision
            else original.get("identity_reason", original["reason"]),
            "assessment_status": "adjudicated" if decision else "retained_independent_assessment",
            "human_review_status": "pending",
            "references": copy.deepcopy(decision["references"] if decision else source_refs),
            "independent_assessment": {
                key: copy.deepcopy(original[key])
                for key in (
                    "relevance",
                    "study_dataset_identity",
                    "analysis_suitability",
                    "reason",
                    "suitability_reason",
                    "identity_reason",
                    "field_constraint_evidence",
                )
                if key in original
            },
            "independent_inspected_references": provenance,
            "prior_relevance_grade": pooling[identifier].get("prior_relevance_grade"),
            "prior_suitable": pooling[identifier].get("prior_suitable"),
            "required_disagreement_reasons": required.get(identifier, []),
        }
        if decision:
            entry["adjudication"] = copy.deepcopy(decision)
        final.append(entry)
    output = {
        "schema_version": 1,
        "artifact_type": "discovery_agent_adjudication",
        "binding": {
            "queue_sha256": queue["queue_sha256"],
            "benchmark_id": queue["binding"]["benchmark_id"],
            "independent_sha256": _sha(independent),
            "submissions_sha256": sorted(reviewers),
        },
        "reviewer": {
            "type": "agent",
            "name": "Source-backed adjudication",
            "submissions": [
                {"sha256": digest, "reviewer": reviewers[digest]} for digest in sorted(reviewers)
            ],
        },
        "human_review_status": "pending",
        "rubric": {
            "relevance": {"0": "irrelevant", "1": "related", "2": "useful", "3": "direct"},
            "identity": sorted(IDENTITIES),
            "analysis_suitability": sorted(SUITABILITIES),
            "scope": "Completed candidate pool only; global relevance judgments remain incomplete. Unknown suitability is neither a verified positive nor a verified negative.",
            "source_policy": "New decisions require literal UTF-8 excerpts and local SHA256 verification. Inherited selections retain their original provenance; transformed selections are not represented as literal excerpts.",
        },
        "counts": {
            "questions": len(questions),
            "pairs": len(final),
            "required_disagreements": len(required),
            "required_disagreement_types": dict(
                sorted(
                    Counter(reason for reasons in required.values() for reason in reasons).items()
                )
            ),
            "reviewed_required_disagreements": len(required),
            "remaining_disagreements": 0,
            "additional_decisions": len(decisions) - len(required),
            "adjudicated_pairs": len(decisions),
            "retained_independent_assessments": len(final) - len(decisions),
            "human_judgments": 0,
            "relevance": dict(sorted(Counter(str(item["relevance"]) for item in final).items())),
            "suitability": dict(
                sorted(Counter(item["analysis_suitability"] for item in final).items())
            ),
            "changes_from_independent": {
                "relevance": sum(
                    item["relevance"] != judgments[item["row_id"]]["relevance"] for item in final
                ),
                "suitability": sum(
                    item["analysis_suitability"]
                    != judgments[item["row_id"]]["analysis_suitability"]
                    for item in final
                ),
                "identity": sum(
                    item["study_dataset_identity"]
                    != judgments[item["row_id"]]["study_dataset_identity"]
                    for item in final
                ),
            },
        },
        "judgments": final,
        "question_decisions": [question_notes[key] for key in sorted(question_notes)],
        "required_sources": [
            {"path": path, "sha256": evidence.files[path]} for path in sorted(evidence.files)
        ],
    }
    output["adjudication_sha256"] = _sha(output)
    return output


def _mean(values: list[float | None]) -> float | None:
    observed = [value for value in values if value is not None]
    return sum(observed) / len(observed) if observed else None


def evaluate_adjudication(queue: dict, adjudication: dict) -> dict:
    """Evaluate unchanged top-ten rankings against source-backed pooled labels."""
    questions, rows, pooling = _queue(queue)
    if (
        not isinstance(adjudication, dict)
        or type(adjudication.get("schema_version")) is not int
        or adjudication.get("schema_version") != 1
        or adjudication.get("artifact_type") != "discovery_agent_adjudication"
        or adjudication.get("adjudication_sha256")
        != _sha({key: value for key, value in adjudication.items() if key != "adjudication_sha256"})
    ):
        raise AdjudicationError("Invalid adjudication content hash")
    _bindings(adjudication.get("binding", {}), queue)
    _agent(adjudication.get("reviewer"))
    judgments = _unique(adjudication.get("judgments"), "row_id", "adjudicated judgment")
    if set(judgments) != set(rows):
        raise AdjudicationError("Final judgments do not cover the exact pool")
    for identifier, judgment in judgments.items():
        _assessment(judgment)
        if any(judgment.get(key) != rows[identifier][key] for key in ("question_id", "dataset_id")):
            raise AdjudicationError("Final judgment identity differs from pool")
    return _evaluate(
        queue,
        questions,
        pooling,
        judgments,
        adjudication["binding"],
        adjudication["adjudication_sha256"],
    )


def evaluate_independent(queue: dict, independent: dict) -> dict:
    """Use the identical pooled metric implementation for the frozen baseline."""
    questions, rows, pooling = _queue(queue)
    if (
        not isinstance(independent, dict)
        or type(independent.get("schema_version")) is not int
        or independent.get("schema_version") != 1
    ):
        raise AdjudicationError("Invalid independent assessment")
    _bindings(independent, queue)
    _agent(independent.get("reviewer"))
    judgments = _unique(independent.get("judgments"), "row_id", "independent judgment")
    if set(judgments) != set(rows):
        raise AdjudicationError("Independent judgments do not cover the exact pool")
    for identifier, judgment in judgments.items():
        _assessment(judgment)
        if any(judgment.get(key) != rows[identifier][key] for key in ("question_id", "dataset_id")):
            raise AdjudicationError("Independent judgment identity differs from pool")
        if "queue_sha256" in judgment or "benchmark_id" in judgment:
            _bindings(judgment, queue)
        if "reviewer" in judgment:
            _agent(judgment["reviewer"])
    return _evaluate(
        queue,
        questions,
        pooling,
        judgments,
        {
            "queue_sha256": queue["queue_sha256"],
            "benchmark_id": queue["binding"]["benchmark_id"],
            "independent_sha256": _sha(independent),
        },
    )


def _evaluate(
    queue: dict,
    questions: dict,
    pooling: dict,
    judgments: dict,
    binding: dict,
    adjudication_hash: str | None = None,
) -> dict:
    legacy = queue.get("selection", {}).get("policy_version", 1) == 1
    methods = {}
    for method in METHODS:
        records = []
        for question_id in sorted(questions):
            pool = [item for item in judgments.values() if item["question_id"] == question_id]
            ranked = sorted(
                (item for item in pool if method in pooling[item["row_id"]]["method_ranks"]),
                key=lambda item: pooling[item["row_id"]]["method_ranks"][method]["rank"],
            )[:10]
            ideal = sorted((item["relevance"] for item in pool), reverse=True)[:10]
            ideal_dcg = sum(
                (2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal)
            )
            dcg = sum(
                (2 ** item["relevance"] - 1) / math.log2(index + 2)
                for index, item in enumerate(ranked)
            )
            useful_pool = sum(item["relevance"] >= 2 for item in pool)
            eligible_pool = sum(item["analysis_suitability"] == "eligible" for item in pool)
            useful = sum(item["relevance"] >= 2 for item in ranked)
            eligible = sum(item["analysis_suitability"] == "eligible" for item in ranked)
            unknown = sum(item["analysis_suitability"] == "unknown" for item in ranked)
            known = len(ranked) - unknown
            metrics = {
                "ndcg@10": dcg / ideal_dcg if ideal_dcg else None,
                "useful_precision@10": useful / 10,
                "direct_precision@10": sum(item["relevance"] == 3 for item in ranked) / 10,
                "eligible_precision@10": eligible / 10,
                "eligibility_precision_known@10": eligible / known if known else None,
                "unknown_suitability_share@10": unknown / 10,
                "unknown_returned_share@10": unknown / len(ranked) if ranked else None,
                "useful_pool_recall@10": useful / useful_pool if useful_pool else None,
                "eligible_pool_recall@10": eligible / eligible_pool if eligible_pool else None,
            }
            records.append(
                {
                    "question_id": question_id,
                    **(
                        {"portsmouth": bool(questions[question_id].get("portsmouth"))}
                        if legacy
                        else {}
                    ),
                    "returned": len(ranked),
                    "useful_pool_positives": useful_pool,
                    "eligible_pool_positives": eligible_pool,
                    "unknown_top10": unknown,
                    "metrics": metrics,
                    "ranking": [
                        {
                            "row_id": item["row_id"],
                            "dataset_id": item["dataset_id"],
                            "rank": pooling[item["row_id"]]["method_ranks"][method]["rank"],
                            "relevance": item["relevance"],
                            "analysis_suitability": item["analysis_suitability"],
                        }
                        for item in ranked
                    ],
                }
            )
        metric_keys = records[0]["metrics"]
        methods[method] = {
            "metrics": {
                key: _mean([row["metrics"][key] for row in records]) for key in metric_keys
            },
            "denominators": {
                key: sum(row["metrics"][key] is not None for row in records) for key in metric_keys
            },
            "questions": records,
        }
        if legacy:
            methods[method]["portsmouth"] = {
                "questions": sum(row["portsmouth"] for row in records),
                "metrics": {
                    key: _mean([row["metrics"][key] for row in records if row["portsmouth"]])
                    for key in metric_keys
                },
                "denominators": {
                    key: sum(
                        row["metrics"][key] is not None for row in records if row["portsmouth"]
                    )
                    for key in metric_keys
                },
            }
        elif queue.get("cohorts"):
            methods[method]["cohorts"] = {}
            for name, identifiers in sorted(queue["cohorts"].items()):
                selected = [row for row in records if row["question_id"] in identifiers]
                methods[method]["cohorts"][name] = {
                    "questions": len(selected),
                    "metrics": {
                        key: _mean([row["metrics"][key] for row in selected]) for key in metric_keys
                    },
                    "denominators": {
                        key: sum(row["metrics"][key] is not None for row in selected)
                        for key in metric_keys
                    },
                }
    result = {
        "schema_version": 1,
        "artifact_type": "discovery_agent_adjudication_evaluation",
        "binding": {
            **copy.deepcopy(binding),
            **({"adjudication_sha256": adjudication_hash} if adjudication_hash else {}),
        },
        "human_review_status": "pending",
        "evaluation_scope": "Descriptive pooled-only evaluation; incomplete global qrels. No catalogue-wide recall or general method superiority claim. Rankings and benchmark inputs are unchanged.",
        "metric_policy": {
            "cutoff": 10,
            "gain": "2^grade - 1",
            "precision": "Useful/direct precision and confirmed eligibility yield use ten slots, including unreturned slots. eligible_precision@10 is confirmed eligibility yield, not an assertion that unknowns fail. eligibility_precision_known@10 excludes unknown suitability from its denominator.",
            "unknown": "Unknown suitability is neither a verified positive nor a verified negative. Unknown suitability share uses ten slots; unknown returned share uses returned results; useful/eligible pool recall uses only confirmed pool positives. Questions with zero denominator have null metrics and are excluded from that macro mean.",
            "ndcg": "Ideal ranking is drawn only from the assessed pool; zero ideal gain yields null.",
        },
        "counts": {"questions": len(questions), "pairs": len(judgments)},
        "methods": methods,
    }
    if not legacy:
        result["policy_version"] = 2
    result["evaluation_sha256"] = _sha(result)
    return result
