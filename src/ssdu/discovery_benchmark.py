"""Frozen, offline discovery experiments; deliberately separate from the live registry.

The lexical, semantic and reciprocal-rank-fusion methods use the same metadata.
Embeddings are supplied artifacts, never generated or downloaded during replay.
Coverage is checked after retrieval and missing metadata remains unknown.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import platform
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

REQUIRED_FILES = ("corpus.json", "questions.json", "evidence.json", "embeddings.npz")
SEARCH_FIELDS = (
    "identifiers",
    "title",
    "variables",
    "concepts",
    "description",
    "coverage",
    "access",
)
DEFAULT_WEIGHTS = (10, 8, 4, 3, 1, 2, 1)
DEFAULT_COVERAGE_POLICY = "declared-geography-v2"
LEGACY_COVERAGE_POLICY = "legacy-v1"
CONSTRAINT_FIELDS = {
    "country",
    "place",
    "period_start",
    "period_end",
    "release",
    "geography_level",
    "geography_vintage",
    "concept",
    "access",
}


class BenchmarkError(ValueError):
    """An artifact is invalid or does not match the frozen experiment."""


def coverage_policy(bundle: BenchmarkBundle) -> str:
    """Missing policy markers identify existing frozen version-1 experiments."""
    policy = bundle.manifest.get("configuration", {}).get("coverage_policy", LEGACY_COVERAGE_POLICY)
    return _coverage_policy(policy)


def _coverage_policy(policy):
    if not isinstance(policy, str) or policy not in {
        DEFAULT_COVERAGE_POLICY,
        LEGACY_COVERAGE_POLICY,
    }:
        raise BenchmarkError("Unsupported coverage_policy")
    return policy


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BenchmarkError(f"{label} must be a list of strings")
    return value


def _records(value: Any, key: str) -> list[dict]:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise BenchmarkError(f"Missing {key} list")
    if type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
        raise BenchmarkError(f"Unsupported {key} schema_version")
    records = value[key]
    seen = set()
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise BenchmarkError(f"Invalid {key} identifier")
        if item["id"] in seen:
            raise BenchmarkError(f"Duplicate {key} identifier: {item['id']}")
        seen.add(item["id"])
    return records


def _references(evidence: list[dict] | dict[str, dict], identifiers: list[str]) -> list[dict]:
    """Carry readable source citations with each row, retaining the authoritative IDs."""
    sources = (
        evidence if isinstance(evidence, dict) else {source["id"]: source for source in evidence}
    )
    return [
        {
            field: sources[identifier].get(field)
            for field in ("id", "type", "title", "url", "published_at")
        }
        for identifier in identifiers
    ]


def _search_fields(document: dict) -> tuple[str, ...]:
    coverage = document.get("coverage", {})
    coverage_text = []
    for field in ("countries", "places", "geography_levels", "geography_vintages"):
        coverage_text.extend(coverage.get(field, []))
    coverage_text.extend(
        str(coverage[field])
        for field in ("period_start", "period_end", "release")
        if coverage.get(field) is not None
    )
    return (
        " ".join(str(document.get(field, "")) for field in ("id", "provider", "native_id")),
        document.get("title", ""),
        " ".join(document.get("variables", [])),
        " ".join(document.get("concepts", [])),
        document.get("description", ""),
        " ".join(coverage_text),
        document.get("access", ""),
    )


def searchable_text(document: dict) -> str:
    """Version-1 input shared by all methods; notes, judgments and evidence are excluded."""
    return "\n".join(
        f"{name}: {value}" for name, value in zip(SEARCH_FIELDS, _search_fields(document))
    )


@dataclass
class BenchmarkBundle:
    root: Path
    manifest: dict
    documents: list[dict]
    questions: list[dict]
    evidence: list[dict]
    document_vectors: np.ndarray
    query_vectors: np.ndarray
    lexical_weights: tuple[float, ...]
    rrf_k: float
    cutoffs: tuple[int, ...]


def _parse_json(data: bytes, label: str) -> dict:
    def reject_constant(value):
        raise BenchmarkError(f"Nonfinite JSON constant in {label}: {value}")

    try:
        value = json.loads(data, parse_constant=reject_constant)
    except ValueError as exc:
        raise BenchmarkError(f"Cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{label} must contain a JSON object")
    return value


def _read_json(path: Path) -> dict:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"Cannot read {path.name}: {exc}") from exc
    return _parse_json(data, path.name)


def _validate_metadata(documents: list[dict], evidence_ids: set[str]) -> None:
    for doc in documents:
        for field in ("provider", "native_id", "title", "description", "access"):
            if not isinstance(doc.get(field), str):
                raise BenchmarkError(f"{doc['id']}.{field} must be a string")
        for field in ("variables", "concepts", "source_urls"):
            _strings(doc.get(field, []), f"{doc['id']}.{field}")
        coverage = doc.get("coverage")
        if not isinstance(coverage, dict):
            raise BenchmarkError(f"{doc['id']}.coverage must be an object")
        for field in ("countries", "places", "geography_levels", "geography_vintages"):
            _strings(coverage.get(field, []), f"{doc['id']}.coverage.{field}")
        for field in ("period_start", "period_end"):
            if coverage.get(field) is not None and type(coverage[field]) is not int:
                raise BenchmarkError(f"{doc['id']}.coverage.{field} must be an integer or null")
        if (
            coverage.get("period_start") is not None
            and coverage.get("period_end") is not None
            and coverage["period_start"] > coverage["period_end"]
        ):
            raise BenchmarkError(f"{doc['id']} has an inverted period")
        if coverage.get("release") is not None and not isinstance(coverage["release"], (str, int)):
            raise BenchmarkError(f"{doc['id']}.coverage.release must be a string, integer or null")
        provenance = doc.get("provenance", {})
        if not isinstance(provenance, dict):
            raise BenchmarkError(f"{doc['id']}.provenance must be an object")
        links = _strings(provenance.get("evidence_ids", []), f"{doc['id']}.provenance.evidence_ids")
        if set(links) - evidence_ids:
            raise BenchmarkError(f"Unknown evidence in document {doc['id']}")


def _validate_questions(
    questions: list[dict], document_ids: set[str], evidence_ids: set[str]
) -> None:
    topic_splits = {}
    for question in questions:
        if "suitability_judged" in question and type(question["suitability_judged"]) is not bool:
            raise BenchmarkError(f"{question['id']}.suitability_judged must be boolean")
        for field in ("text", "origin", "split"):
            if not isinstance(question.get(field), str) or not question[field].strip():
                raise BenchmarkError(f"{question['id']}.{field} must be a nonempty string")
        if question["split"] not in {"dev", "test"}:
            raise BenchmarkError(f"{question['id']}.split must be dev or test")
        if "topic_group" in question:
            topic = question["topic_group"]
            if not isinstance(topic, str) or not topic.strip():
                raise BenchmarkError(f"{question['id']}.topic_group must be a nonempty string")
            if topic in topic_splits and topic_splits[topic] != question["split"]:
                raise BenchmarkError(f"topic_group spans dev and test: {topic}")
            topic_splits[topic] = question["split"]
        links = _strings(question.get("evidence_ids", []), f"{question['id']}.evidence_ids")
        if set(links) - evidence_ids:
            raise BenchmarkError(f"Unknown evidence in question {question['id']}")
        relevance = question.get("relevance", {})
        if not isinstance(relevance, dict) or set(relevance) - document_ids:
            raise BenchmarkError(f"Unknown relevance document in {question['id']}")
        if any(type(grade) is not int or grade not in range(4) for grade in relevance.values()):
            raise BenchmarkError(f"Invalid relevance grade in {question['id']}")
        suitable = _strings(
            question.get("expected_suitable", []), f"{question['id']}.expected_suitable"
        )
        if set(suitable) - document_ids or any(
            relevance.get(doc_id, 0) == 0 for doc_id in suitable
        ):
            raise BenchmarkError(f"Unknown or irrelevant suitable document in {question['id']}")
        groups = question.get("required_groups", [])
        if not isinstance(groups, list):
            raise BenchmarkError(f"Invalid required_groups in {question['id']}")
        for group in groups:
            if (
                not _strings(group, f"{question['id']}.required_groups")
                or set(group) - document_ids
            ):
                raise BenchmarkError(f"Empty or unknown required group in {question['id']}")
        constraints = question.get("constraints", {})
        if not isinstance(constraints, dict) or set(constraints) - CONSTRAINT_FIELDS:
            raise BenchmarkError(f"Unsupported constraint in {question['id']}")
        for field, value in constraints.items():
            if field in {"period_start", "period_end"}:
                if type(value) is not int:
                    raise BenchmarkError(f"{question['id']}.{field} must be an integer")
            elif field in {"release", "geography_vintage"}:
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise BenchmarkError(f"{question['id']}.{field} must be a string or integer")
            elif not isinstance(value, str) or not value.strip():
                raise BenchmarkError(f"{question['id']}.{field} must be a nonempty string")
        if (
            "period_start" in constraints
            and "period_end" in constraints
            and constraints["period_start"] > constraints["period_end"]
        ):
            raise BenchmarkError(f"Inverted requested period in {question['id']}")


def load_bundle(path: str | Path) -> BenchmarkBundle:
    """Verify all frozen bytes, references, configuration and vector rows before search."""
    root = Path(path).resolve()
    manifest = _read_json(root / "manifest.json")
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        raise BenchmarkError("Unsupported manifest schema_version")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(REQUIRED_FILES) - set(files):
        raise BenchmarkError("Manifest must hash all required files")
    artifacts = {}
    for name, digest in files.items():
        if (
            not isinstance(name, str)
            or not name
            or name in {".", "..", "manifest.json"}
            or Path(name).name != name
            or "/" in name
            or "\\" in name
        ):
            raise BenchmarkError("Manifest filenames must be safe bundle basenames")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BenchmarkError(f"Invalid SHA256 for {name}")
        candidate = root / name
        if candidate.is_symlink():
            raise BenchmarkError(f"Symlink artifacts are not permitted: {name}")
        try:
            data = candidate.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
        except OSError as exc:
            raise BenchmarkError(f"Missing artifact: {name}") from exc
        if actual != digest:
            raise BenchmarkError(f"SHA256 mismatch: {name}")
        artifacts[name] = data
    documents = _records(_parse_json(artifacts["corpus.json"], "corpus.json"), "documents")
    questions = _records(_parse_json(artifacts["questions.json"], "questions.json"), "questions")
    evidence = _records(_parse_json(artifacts["evidence.json"], "evidence.json"), "sources")
    if not documents or not questions:
        raise BenchmarkError("A benchmark needs documents and questions")
    document_ids = {doc["id"] for doc in documents}
    query_ids = {query["id"] for query in questions}
    evidence_ids = {source["id"] for source in evidence}
    _validate_metadata(documents, evidence_ids)
    _validate_questions(questions, document_ids, evidence_ids)
    for record in documents + questions:
        identifiers = record.get(
            "evidence_ids", record.get("provenance", {}).get("evidence_ids", [])
        )
        if "references" in record and record["references"] != _references(evidence, identifiers):
            raise BenchmarkError(f"Embedded references disagree with evidence: {record['id']}")
    model = manifest.get("model", {})
    if not isinstance(model, dict):
        raise BenchmarkError("model must be an object")
    input_hashes = model.get("input_hashes", {})
    if not isinstance(input_hashes, dict) or any(
        name not in {"corpus.json", "questions.json"} or digest != files[name]
        for name, digest in input_hashes.items()
    ):
        raise BenchmarkError("Embedding input hashes do not match frozen metadata")
    configuration = manifest.get("configuration", {})
    if not isinstance(configuration, dict):
        raise BenchmarkError("configuration must be an object")
    if str(configuration.get("search_text_version", "1")) != "1":
        raise BenchmarkError("Unsupported search_text_version")
    _coverage_policy(configuration.get("coverage_policy", LEGACY_COVERAGE_POLICY))
    weights = configuration.get("lexical_weights", DEFAULT_WEIGHTS)
    if (
        not isinstance(weights, (list, tuple))
        or len(weights) != len(SEARCH_FIELDS)
        or any(type(w) not in {int, float} or not math.isfinite(w) or w < 0 for w in weights)
        or not any(weights)
    ):
        raise BenchmarkError("lexical_weights must contain seven finite nonnegative weights")
    rrf_k = configuration.get("rrf_k", 60)
    if type(rrf_k) not in {int, float} or not math.isfinite(rrf_k) or rrf_k <= 0:
        raise BenchmarkError("rrf_k must be positive and finite")
    cutoffs = configuration.get("metric_cutoffs", [5, 10])
    if (
        not isinstance(cutoffs, list)
        or not cutoffs
        or any(type(k) is not int or not 1 <= k <= 1000 for k in cutoffs)
    ):
        raise BenchmarkError("metric_cutoffs must be positive integers up to 1000")
    try:
        archive = np.load(io.BytesIO(artifacts["embeddings.npz"]), allow_pickle=False)
        if not isinstance(archive, np.lib.npyio.NpzFile):
            raise BenchmarkError("Embeddings must be an NPZ archive")
        with archive:
            if len(archive.files) != 4 or set(archive.files) != {
                "doc_ids",
                "query_ids",
                "document_vectors",
                "query_vectors",
            }:
                raise BenchmarkError("Unexpected embedding archive fields")
            ordered_doc_ids = archive["doc_ids"]
            ordered_query_ids = archive["query_ids"]
            for labels, expected, name in (
                (ordered_doc_ids, document_ids, "doc_ids"),
                (ordered_query_ids, query_ids, "query_ids"),
            ):
                if (
                    labels.ndim != 1
                    or labels.dtype.kind not in {"U", "S"}
                    or len(labels) != len(expected)
                    or set(labels.tolist()) != expected
                ):
                    raise BenchmarkError(f"Embedding {name} do not match metadata IDs")
            vectors = []
            for field, labels, records in (
                ("document_vectors", ordered_doc_ids, documents),
                ("query_vectors", ordered_query_ids, questions),
            ):
                values = archive[field]
                if (
                    values.ndim != 2
                    or values.shape[0] != len(labels)
                    or values.shape[1] == 0
                    or values.dtype.kind != "f"
                    or not np.isfinite(values).all()
                ):
                    raise BenchmarkError(f"Invalid {field}: shape, floating dtype or finite values")
                values = values.astype(np.float64)
                norms = np.sqrt(np.sum(values * values, axis=1))
                if not np.isfinite(norms).all() or (norms == 0).any():
                    raise BenchmarkError(f"Invalid {field}: zero or nonfinite norm")
                positions = {doc_id: i for i, doc_id in enumerate(labels.tolist())}
                values = values / norms[:, None]
                vectors.append(values[[positions[record["id"]] for record in records]])
            if vectors[0].shape[1] != vectors[1].shape[1]:
                raise BenchmarkError("Embedding dimensions do not match")
            if "dimensions" in model and model["dimensions"] != vectors[0].shape[1]:
                raise BenchmarkError("Model dimensions disagree with embedding artifact")
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as exc:
        if isinstance(exc, BenchmarkError):
            raise
        raise BenchmarkError(f"Cannot load safe embedding archive: {exc}") from exc
    return BenchmarkBundle(
        root,
        manifest,
        documents,
        questions,
        evidence,
        vectors[0],
        vectors[1],
        tuple(weights),
        float(rrf_k),
        tuple(sorted(set(cutoffs))),
    )


def _normal(value: Any) -> str:
    return " ".join(str(value).casefold().replace("_", " ").replace("-", " ").split())


def _country(value: str) -> str:
    value = _normal(value)
    return {
        "uk": "united kingdom",
        "u.k.": "united kingdom",
        "gb": "united kingdom",
        "great britain": "great britain",
        "cymru": "wales",
        "eng": "england",
    }.get(value, value)


def _known(values) -> list:
    return [
        value
        for value in values
        if _normal(value) not in {"", "unknown", "unspecified", "not specified", "not supplied"}
    ]


def _legacy_place_check(coverage, requested, places, record):
    """Historical experiment policy; retain its decisions and exact reason strings."""
    portsmouth = {"portsmouth", "e06000044"}
    levels = {_normal(level) for level in coverage.get("geography_levels", [])}
    all_authorities = "all england local authorities" in places and bool(
        levels & {"lad", "local authority", "local authority district", "local authorities"}
    )
    if requested in places or (requested in portsmouth and places & portsmouth):
        record("place", "eligible", "Requested place is explicitly listed")
    elif requested in portsmouth and all_authorities:
        record("place", "eligible", "All England local authorities explicitly include Portsmouth")
    else:
        countries = {_country(country) for country in _known(coverage.get("countries", []))}
        if (
            requested in portsmouth
            and countries
            and not countries & {"england", "united kingdom", "great britain"}
        ):
            record("place", "ineligible", "Portsmouth is outside declared country coverage")
        elif places and not any(place.startswith("all ") for place in places):
            record("place", "ineligible", "Requested place is outside explicitly listed places")
        else:
            record("place", "unknown", "Metadata does not establish requested place coverage")


def _evidenced_place(document, constraints, requested, places):
    """Accept explicit aliases/membership bound to retained sources and declared vintage."""
    coverage = document.get("coverage", {})
    known_sources = set(document.get("provenance", {}).get("evidence_ids", []))
    evidence = coverage.get("place_evidence", [])
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict):
            continue
        names = (
            [item.get("place"), *item.get("aliases", [])]
            if isinstance(item.get("aliases", []), list)
            else []
        )
        sources = item.get("evidence_ids", [])
        if (
            not names
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) or source not in known_sources for source in sources)
        ):
            continue
        names = {_normal(name) for name in names}
        if requested not in names:
            continue
        vintage = item.get("geography_vintage")
        if "geography_vintage" in constraints and (
            vintage is None or _normal(vintage) != _normal(constraints["geography_vintage"])
        ):
            continue
        if names & places:
            return True
        membership = item.get("member_of", [])
        if not isinstance(membership, list) or any(
            not isinstance(value, str) for value in membership
        ):
            continue
        level = item.get("geography_level")
        if (
            {_normal(value) for value in membership} & places
            and vintage is not None
            and _normal(vintage)
            in {_normal(value) for value in coverage.get("geography_vintages", [])}
            and isinstance(level, str)
            and _normal(level) in {_normal(value) for value in coverage.get("geography_levels", [])}
        ):
            return True
    return False


def check_coverage(document: dict, constraints: dict, *, policy=DEFAULT_COVERAGE_POLICY) -> dict:
    """Tri-state declared coverage; broad country/scope labels do not prove local membership.

    The generic policy accepts exact places and source-bound ``place_evidence``
    aliases or versioned membership. Frozen manifests without a policy use the
    isolated legacy policy through ``coverage_policy``.
    """
    _coverage_policy(policy)
    if not isinstance(constraints, dict) or set(constraints) - CONSTRAINT_FIELDS:
        raise BenchmarkError("Unsupported coverage constraint")
    coverage = document.get("coverage", {})
    checks = []

    def record(field: str, status: str, detail: str):
        checks.append({"field": field, "status": status, "reason": detail})

    if "country" in constraints:
        requested = _country(constraints["country"])
        countries = {_country(country) for country in _known(coverage.get("countries", []))}
        constituents = {"england", "wales", "scotland", "northern ireland"}
        if not countries:
            record("country", "unknown", "Country coverage is missing")
        elif (
            requested in countries
            or (requested in constituents and "united kingdom" in countries)
            or (requested == "united kingdom" and constituents <= countries)
            or (requested in {"england", "wales", "scotland"} and "great britain" in countries)
        ):
            record("country", "eligible", "Requested country is within declared coverage")
        else:
            record("country", "ineligible", "Requested country is outside declared coverage")
    if "place" in constraints:
        requested = _normal(constraints["place"])
        places = {_normal(place) for place in _known(coverage.get("places", []))}
        if policy == LEGACY_COVERAGE_POLICY:
            _legacy_place_check(coverage, requested, places, record)
        elif requested in places:
            record("place", "eligible", "Requested place is explicitly listed")
        elif _evidenced_place(document, constraints, requested, places):
            record(
                "place", "eligible", "Requested place matches retained alias or membership evidence"
            )
        elif places and not any(place.startswith("all ") for place in places):
            record("place", "ineligible", "Requested place is outside explicitly listed places")
        else:
            record("place", "unknown", "Metadata does not establish requested place coverage")
    for field in ("period_start", "period_end"):
        if field not in constraints:
            continue
        requested = constraints[field]
        lower, upper = coverage.get("period_start"), coverage.get("period_end")
        if lower is not None and requested < lower or upper is not None and requested > upper:
            record(field, "ineligible", "Requested year is outside declared observation period")
        elif lower is None or upper is None:
            record(field, "unknown", "Observation period bounds are missing")
        else:
            record(field, "eligible", "Requested year is within declared observation period")
    for field, available in (
        ("release", [coverage["release"]] if coverage.get("release") is not None else []),
        ("geography_level", coverage.get("geography_levels", [])),
        ("geography_vintage", coverage.get("geography_vintages", [])),
        ("concept", document.get("concepts", [])),
        ("access", [document["access"]] if document.get("access") else []),
    ):
        if field not in constraints:
            continue
        available = _known(available)
        if not available:
            record(field, "unknown", f"{field} metadata is missing")
        elif _normal(constraints[field]) in {_normal(value) for value in available}:
            record(field, "eligible", f"Requested {field} matches declared metadata")
        else:
            record(field, "ineligible", f"Requested {field} differs from declared metadata")
    statuses = {check["status"] for check in checks}
    status = (
        "ineligible"
        if "ineligible" in statuses
        else ("unknown" if "unknown" in statuses else "eligible")
    )
    return {"status": status, "reasons": [check["reason"] for check in checks], "checks": checks}


@lru_cache(maxsize=8192)
def _identifier_pattern(identifier: str):
    # Keep compiled patterns beyond re's small global cache. Provider inventories
    # otherwise recompile thousands of unchanged identifiers for every question.
    return re.compile(r"(?<!\w)" + re.escape(identifier) + r"(?!\w)", re.IGNORECASE)


def _lexical(bundle: BenchmarkBundle, question: dict) -> list[tuple[str, float]]:
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", question["text"].casefold())))[:128]
    if not terms:
        return []
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE documents USING fts5("
            + ",".join(SEARCH_FIELDS)
            + ", tokenize='unicode61 remove_diacritics 2')"
        )
        connection.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
            [_search_fields(document) for document in bundle.documents],
        )
        query = " OR ".join('"' + term + '"' for term in terms)
        expression = "bm25(documents," + ",".join(str(w) for w in bundle.lexical_weights) + ")"
        rows = connection.execute(
            f"SELECT rowid, {expression} FROM documents WHERE documents MATCH ?", (query,)
        ).fetchall()
    ranked = []
    for row, score in rows:
        document = bundle.documents[row - 1]
        exact = any(
            identifier and _identifier_pattern(identifier).search(question["text"])
            for identifier in (document["id"], document["native_id"])
        )
        ranked.append((document["id"], round(-float(score) + (1_000_000 if exact else 0), 12)))
    return sorted(ranked, key=lambda row: (-row[1], row[0]))


def _semantic(bundle: BenchmarkBundle, question: dict) -> list[tuple[str, float]]:
    positions = {query["id"]: i for i, query in enumerate(bundle.questions)}
    query = bundle.query_vectors[positions[question["id"]]]
    # Fixed reduction order avoids BLAS-dependent threading in replay.
    scores = np.sum(bundle.document_vectors * query, axis=1)
    return sorted(
        (
            (document["id"], round(float(score), 12))
            for document, score in zip(bundle.documents, scores)
        ),
        key=lambda row: (-row[1], row[0]),
    )


def rank(bundle: BenchmarkBundle, question: dict | str, method: str) -> list[dict]:
    """Rank a frozen question; eligibility is disclosed rather than silently inferred."""
    questions = {q["id"]: q for q in bundle.questions}
    query_id = question if isinstance(question, str) else question.get("id")
    if query_id not in questions or (
        isinstance(question, dict) and question != questions[query_id]
    ):
        raise BenchmarkError("Ranking requires an unchanged frozen question")
    question = questions[query_id]
    if method == "lexical":
        ranked = _lexical(bundle, question)
    elif method == "semantic":
        ranked = _semantic(bundle, question)
    elif method == "hybrid":
        scores: dict[str, float] = {}
        for branch in (_lexical(bundle, question), _semantic(bundle, question)):
            for position, (doc_id, _) in enumerate(branch, 1):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (bundle.rrf_k + position)
        ranked = sorted(
            ((doc_id, round(score, 12)) for doc_id, score in scores.items()),
            key=lambda row: (-row[1], row[0]),
        )
    else:
        raise BenchmarkError(f"Unknown method: {method}")
    documents = {document["id"]: document for document in bundle.documents}
    evidence = {source["id"]: source for source in bundle.evidence}
    return [
        {
            "id": doc_id,
            "title": documents[doc_id]["title"],
            "provider": documents[doc_id]["provider"],
            "native_id": documents[doc_id]["native_id"],
            "references": _references(
                evidence, documents[doc_id].get("provenance", {}).get("evidence_ids", [])
            ),
            "score": score,
            **check_coverage(
                documents[doc_id], question.get("constraints", {}), policy=coverage_policy(bundle)
            ),
        }
        for doc_id, score in ranked
    ]


def _question_metrics(question: dict, ranked: list[dict], cutoffs: tuple[int, ...]) -> dict:
    relevance = question.get("relevance", {})
    relevant = {doc_id for doc_id, grade in relevance.items() if grade > 0}
    suitable = set(question.get("expected_suitable", []))
    suitability_judged = question.get("suitability_judged", True)
    groups = question.get("required_groups", [])
    identifiers = [entry["id"] for entry in ranked]
    eligible = [entry["id"] for entry in ranked if entry["status"] == "eligible"]
    first_relevant = next(
        (i for i, doc_id in enumerate(identifiers, 1) if doc_id in relevant), None
    )
    first_suitable = next(
        (
            i
            for i, entry in enumerate(ranked, 1)
            if entry["status"] == "eligible" and entry["id"] in suitable
        ),
        None,
    )
    metrics: dict[str, float | int | None] = {
        "mrr": 1 / first_relevant if first_relevant else (0.0 if relevant else None),
        "first_suitable_reciprocal_rank": 1 / first_suitable
        if first_suitable and suitability_judged
        else (0.0 if suitable and suitability_judged else None),
    }
    for cutoff in cutoffs:
        top = ranked[:cutoff]
        raw_ids = set(identifiers[:cutoff])
        eligible_ids = set(eligible[:cutoff])
        ideal = sorted(relevance.values(), reverse=True)[:cutoff]
        ideal_dcg = sum((2**grade - 1) / math.log2(i + 2) for i, grade in enumerate(ideal))
        dcg = sum(
            (2 ** relevance.get(entry["id"], 0) - 1) / math.log2(i + 2)
            for i, entry in enumerate(top)
        )
        metrics.update(
            {
                f"ndcg@{cutoff}": dcg / ideal_dcg if ideal_dcg else None,
                f"recall@{cutoff}": len(raw_ids & relevant) / len(relevant) if relevant else None,
                f"suitable_recall@{cutoff}": len(eligible_ids & suitable) / len(suitable)
                if suitable and suitability_judged
                else None,
                f"full_collection_coverage@{cutoff}": float(
                    all(raw_ids & set(group) for group in groups)
                )
                if groups
                else None,
                f"eligible_collection_coverage@{cutoff}": float(
                    all(eligible_ids & set(group) for group in groups)
                )
                if groups and suitability_judged
                else None,
                f"known_violations@{cutoff}": sum(entry["status"] == "ineligible" for entry in top),
                f"unknown_coverage@{cutoff}": sum(entry["status"] == "unknown" for entry in top),
                f"constraint_violation_rate@{cutoff}": sum(
                    entry["status"] == "ineligible" for entry in top
                )
                / len(top)
                if top
                else 0.0,
                f"no_answer_eligible_results@{cutoff}": len(eligible[:cutoff])
                if not suitable and suitability_judged
                else None,
            }
        )
    return {
        key: round(value, 12) if isinstance(value, float) else value
        for key, value in metrics.items()
    }


def _aggregate(rows: list[dict]) -> dict:
    keys = sorted({key for row in rows for key in row["metrics"]})
    result = {}
    for key in keys:
        values = [row["metrics"][key] for row in rows if row["metrics"][key] is not None]
        result[key] = {
            "mean": round(sum(values) / len(values), 12) if values else None,
            "questions": len(values),
        }
    return result


def run_benchmark(path: str | Path | BenchmarkBundle) -> dict:
    """Return a reproducible report. File locations and timings are outside its digest."""
    bundle = path if isinstance(path, BenchmarkBundle) else load_bundle(path)
    max_cutoff = max(bundle.cutoffs)
    methods = {}
    for method in ("lexical", "semantic", "hybrid"):
        rows = []
        for question in sorted(bundle.questions, key=lambda item: item["id"]):
            ranked = rank(bundle, question, method)
            rows.append(
                {
                    "question_id": question["id"],
                    "text": question["text"],
                    "references": _references(bundle.evidence, question.get("evidence_ids", [])),
                    "generation": question.get("generation", {"method": "unspecified"}),
                    "split": question["split"],
                    "origin": question["origin"],
                    "metrics": _question_metrics(question, ranked, bundle.cutoffs),
                    "ranking": ranked[:max_cutoff],
                    "eligible_ranking": [
                        entry for entry in ranked if entry["status"] == "eligible"
                    ][:max_cutoff],
                }
            )
        methods[method] = {
            "overall": _aggregate(rows),
            "by_split": {
                split: _aggregate([row for row in rows if row["split"] == split])
                for split in ("dev", "test")
            },
            "by_origin": {
                origin: _aggregate([row for row in rows if row["origin"] == origin])
                for origin in sorted({row["origin"] for row in rows})
            },
            "questions": rows,
        }
    origins = {}
    for question in bundle.questions:
        origins[question["origin"]] = origins.get(question["origin"], 0) + 1
    scoring = {
        "search_text_version": "1",
        "lexical_fields": list(SEARCH_FIELDS),
        "lexical_weights": list(bundle.lexical_weights),
        "rrf_k": bundle.rrf_k,
        "metric_cutoffs": list(bundle.cutoffs),
        "lexical_query": "OR Unicode tokens (maximum 128)",
        "identifier_boost": 1_000_000,
        "semantic": "cosine on frozen vectors",
        "ties": "document ID ascending",
        "score_decimal_places": 12,
        "coverage": "post-retrieval tri-state checks; eligible ranking excludes unknown",
    }
    deterministic = {
        "schema_version": 1,
        "benchmark_id": hashlib.sha256(_canonical(bundle.manifest)).hexdigest(),
        "manifest": bundle.manifest,
        "counts": {
            "documents": len(bundle.documents),
            "questions": len(bundle.questions),
            "evidence_sources": len(bundle.evidence),
            "question_origins": origins,
            "no_relevant_answer_questions": sum(
                not any(q.get("relevance", {}).values()) for q in bundle.questions
            ),
            "no_suitable_answer_questions": sum(
                not q.get("expected_suitable") and q.get("suitability_judged", True)
                for q in bundle.questions
            ),
        },
        "scoring": scoring,
        "methods": methods,
    }
    if any("suitability_judged" in q for q in bundle.questions):
        deterministic["counts"]["unjudged_suitability_questions"] = sum(
            not q.get("suitability_judged", True) for q in bundle.questions
        )
        for method in deterministic["methods"].values():
            by_id = {q["id"]: q for q in bundle.questions}
            for row in method["questions"]:
                question = by_id[row["question_id"]]
                row["suitability_judged"] = question.get("suitability_judged", True)
                if "judgment_provenance" in question:
                    row["judgment_provenance"] = question["judgment_provenance"]
    return {
        **deterministic,
        "replay_sha256": hashlib.sha256(_canonical(deterministic)).hexdigest(),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "numpy": np.__version__,
        },
    }
