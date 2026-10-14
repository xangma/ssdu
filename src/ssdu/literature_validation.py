"""Offline invariants for source-grounded UK paper–dataset review ledgers.

Literal grounding and catalogue identity are checked here. Whether a passage
actually establishes analytical use remains a review judgment; this module
never promotes an agent judgment to independent human validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from urllib.parse import urlsplit

from .discovery_benchmark import CONSTRAINT_FIELDS
from .literature_sources import LiteratureSourceError, reconstruct_abstract
from .literature_store import LiteratureError, _safe_name, validate_document

DECISIONS = {"supported_use", "citation_only", "mention_only", "rejected", "unresolved"}
BASES = {"publication_abstract", "publication_full_text", "title_only"}
LEVELS = {"family", "specific_dataset", "specific_release"}


class LinkValidationError(ValueError):
    """A review is incomplete, inconsistent or not grounded in supplied bytes."""


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise LinkValidationError(f"{label} must be a nonempty string")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise LinkValidationError(f"{label} must be a list")
    return value


def _finite_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise LinkValidationError("Reviews must contain finite JSON values") from exc


def _no_human_claim(value):
    if isinstance(value, dict):
        if (
            "independently_human_validated" in value
            and value["independently_human_validated"] is not False
        ):
            raise LinkValidationError(
                "Agent/source review cannot claim independent human validation"
            )
        if value.get("status") == "human_reviewed":
            raise LinkValidationError("Agent/source review cannot claim human_reviewed")
        for child in value.values():
            _no_human_claim(child)
    elif isinstance(value, list):
        for child in value:
            _no_human_claim(child)


def _refs(row, sources, label, *, required=()):
    refs = _list(row.get("references"), f"{label}.references")
    if not refs:
        raise LinkValidationError(f"{label} requires references")
    identifiers = set()
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("id") not in sources:
            raise LinkValidationError(f"Unknown {label} reference")
        identifier = ref["id"]
        if identifier in identifiers:
            raise LinkValidationError(f"Duplicate {label} reference")
        identifiers.add(identifier)
        source = sources[identifier]
        for key in ("title", "url"):
            if ref.get(key) != source[key]:
                raise LinkValidationError(f"{label} reference {key} disagrees with source")
        for key in ("type", "published_at"):
            if key in ref and key in source and ref[key] != source[key]:
                raise LinkValidationError(f"{label} reference {key} disagrees with source")
    if set(required) - identifiers:
        raise LinkValidationError(f"{label} must preserve original paper references")
    return identifiers


def _references(identifiers, sources):
    return [
        {
            key: sources[identifier].get(key)
            for key in ("id", "title", "url", "type", "published_at")
        }
        for identifier in sorted(set(identifiers))
    ]


def _constraints(value):
    if not isinstance(value, dict) or set(value) - CONSTRAINT_FIELDS:
        raise LinkValidationError("Unsupported question constraints")
    for key, item in value.items():
        if key in {"period_start", "period_end"}:
            if type(item) is not int:
                raise LinkValidationError(f"Constraint {key} must be an integer")
        elif key in {"release", "geography_vintage"}:
            if type(item) not in {int, str} or isinstance(item, str) and not item.strip():
                raise LinkValidationError(f"Constraint {key} must be a string or integer")
        else:
            _text(item, f"Constraint {key}")
    if value.get("period_start", -float("inf")) > value.get("period_end", float("inf")):
        raise LinkValidationError("Inverted question period")


def _pointer(value, pointer):
    _text(pointer, "JSON pointer")
    if not pointer.startswith("/"):
        raise LinkValidationError("Invalid captured JSON pointer")
    try:
        for token in pointer[1:].split("/"):
            if re.search(r"~(?![01])", token):
                raise ValueError("Invalid JSON pointer escape")
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError("Invalid array index")
                value = value[int(token)]
            else:
                value = value[token]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LinkValidationError("Captured JSON pointer does not resolve") from exc
    return value


def _resource(resources, source, path):
    try:
        _safe_name(path)
    except LiteratureError as exc:
        raise LinkValidationError(str(exc)) from exc
    if path not in source.get("resource_paths", []) or path not in resources:
        raise LinkValidationError("Evidence resource is missing or not bound to its source")
    try:
        return resources[path].decode("utf-8")
    except UnicodeError as exc:
        raise LinkValidationError("Evidence resource is not readable UTF-8") from exc


def _alternative_publication_field(paper, source, resources, field):
    """A deduplicated paper can occur in more than one captured search page."""
    identifiers = set(paper.get("openalex_ids", []))
    if paper["id"].startswith("openalex:"):
        identifiers.add("https://openalex.org/" + paper["id"].removeprefix("openalex:"))
    for path in source.get("resource_paths", []):
        if not path.endswith(".json") or path.endswith(".capture.json"):
            continue
        try:
            capture = json.loads(_resource(resources, source, path))
        except (ValueError, LinkValidationError):
            continue
        if not isinstance(capture, dict) or not isinstance(capture.get("results"), list):
            continue
        for position, work in enumerate(capture["results"]):
            if not isinstance(work, dict) or work.get("id") not in identifiers:
                continue
            try:
                value = (
                    reconstruct_abstract(work.get("abstract_inverted_index"))
                    if field == "abstract"
                    else work.get("title")
                )
            except LiteratureSourceError:
                continue
            if value == paper.get(field):
                return {"resource_path": path, "json_pointer": f"/results/{position}"}
    raise LinkValidationError(
        "Publication evidence disagrees with field provenance and captured source"
    )


def _evidence(item, *, paper, sources, resources, provider_datasets, provider=False, fallbacks=()):
    if not isinstance(item, dict):
        raise LinkValidationError("Evidence must be an object")
    item = copy.deepcopy(item)
    source_id = item.get("source_id")
    if source_id not in sources:
        raise LinkValidationError("Evidence refers to an unknown source")
    source = sources[source_id]
    if item.get("url") != source["url"]:
        raise LinkValidationError("Evidence URL disagrees with its source")
    if not isinstance(item.get("location"), (str, dict)) or not item["location"]:
        raise LinkValidationError("Evidence requires a location")
    field = item.get("field")
    if not provider and not item.get("excerpt"):
        matches = [
            candidate
            for candidate in fallbacks
            if all(
                item.get(key) == candidate.get(key) for key in ("source_id", "field", "location")
            )
            and ("span" not in item or item["span"] == candidate.get("span"))
        ]
        if len(matches) != 1:
            raise LinkValidationError("Publication evidence must supply a literal excerpt")
        item["excerpt"] = matches[0]["excerpt"]
    if field in {"abstract", "title"}:
        if provider or source_id not in paper["source_ids"]:
            raise LinkValidationError("Publication evidence is not bound to the paper")
        provenance = paper.get("field_provenance", {}).get(field)
        text = paper.get(field)
        if not isinstance(text, str):
            raise LinkValidationError("Publication evidence field is unavailable")
        verified_location = {"field": field, "paper_id": paper["id"]}
        if provenance and provenance.get("source_id") != source_id:
            verified_location.update(
                _alternative_publication_field(paper, source, resources, field)
            )
    elif field == "captured_text":
        path = source.get("text_resource")
        text = _resource(resources, source, path)
        if (
            isinstance(item["location"], dict)
            and item["location"].get("resource_path", path) != path
        ):
            raise LinkValidationError("Captured text location disagrees with source text_resource")
        verified_location = {"field": field, "resource_path": path}
    elif provider and field in {"captured_json", "catalogue"}:
        location = item["location"]
        if field == "catalogue":
            location = {"resource_path": item.get("resource"), "json_pointer": location}
        if not isinstance(location, dict):
            raise LinkValidationError("Captured JSON evidence requires a resource and pointer")
        text = _resource(resources, source, location.get("resource_path"))
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise LinkValidationError("Captured evidence is not valid JSON") from exc
        value = _pointer(value, location.get("json_pointer"))
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        verified_location = dict(location, field=field)
    elif provider and field is None:
        location = item["location"]
        prefix = "/datasets/"
        if not isinstance(location, str) or not location.startswith(prefix):
            raise LinkValidationError("Provider evidence requires a captured or catalogue pointer")
        dataset = provider_datasets.get(location[len(prefix) :])
        if dataset is None or source_id not in dataset["source_ids"]:
            raise LinkValidationError("Provider evidence is not bound to a catalogue dataset")
        text = json.dumps(dataset, ensure_ascii=False, sort_keys=True, allow_nan=False)
        verified_location = {"field": "catalogue_dataset", "dataset_id": dataset["id"]}
    else:
        raise LinkValidationError("Unsupported evidence field")
    excerpt = item.get("excerpt")
    if excerpt is not None:
        _text(excerpt, "Evidence excerpt")
        starts = [match.start() for match in re.finditer(re.escape(excerpt), text)]
        if not starts:
            raise LinkValidationError(
                "Evidence excerpt is not a literal substring of supplied text"
            )
        verified_location["verified_spans"] = [
            {"start": start, "end": start + len(excerpt), "unit": "Unicode code points"}
            for start in starts
        ]
    elif not provider:
        raise LinkValidationError("Publication evidence requires an excerpt")
    verified_location["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    item["verified_location"] = verified_location
    item["references"] = _references([source_id], sources)
    return item, text


def validate_link_reviews(
    source_document, provider_document, reviews, *, source_snapshot_id, resources
):
    """Validate a complete ledger and return deterministic, referenced proposals.

    ``resources`` maps portable resource paths to already captured bytes. Snapshot
    archive integrity must also be checked by the caller; no network, file read,
    inference model or independent human verification is performed here.
    """
    if not isinstance(source_snapshot_id, str) or not re.fullmatch(
        r"[a-f0-9]{64}", source_snapshot_id
    ):
        raise LinkValidationError("Invalid source_snapshot_id")
    if not isinstance(resources, Mapping) or any(
        not isinstance(path, str) or not isinstance(raw, bytes) for path, raw in resources.items()
    ):
        raise LinkValidationError("Resources must map paths to bytes")
    try:
        source_document = validate_document(source_document, resources)
        provider_document = validate_document(provider_document, resources)
    except LiteratureError as exc:
        raise LinkValidationError(str(exc)) from exc
    reviews = _finite_copy(_list(reviews, "reviews"))
    _no_human_claim(reviews)
    sources = {}
    for source in source_document["sources"] + provider_document["sources"]:
        if source["id"] in sources and source != sources[source["id"]]:
            raise LinkValidationError("Conflicting source descriptors")
        sources[source["id"]] = source
    for review in reviews:
        if (
            not isinstance(review, dict)
            or type(review.get("schema_version")) is not int
            or review["schema_version"] != 1
        ):
            raise LinkValidationError("Unsupported review schema_version")
        if review.get("source_snapshot_id") != source_snapshot_id:
            raise LinkValidationError("Review source snapshot mismatch")
        _text(review.get("family_id"), "Review family_id")
        for source in _list(review.get("sources", []), "review.sources"):
            if not isinstance(source, dict):
                raise LinkValidationError("Invalid review source")
            for key in ("id", "title", "url"):
                _text(source.get(key), f"Source {key}")
            url = urlsplit(source["url"])
            if (
                url.scheme not in {"http", "https", "urn"}
                or (url.scheme != "urn" and not url.netloc)
                or not url.path
                or any(char.isspace() for char in source["url"])
            ):
                raise LinkValidationError("Invalid review source URL")
            if source["id"] in sources and sources[source["id"]] != source:
                raise LinkValidationError("Conflicting review source descriptors")
            paths = _list(source.get("resource_paths", []), "source.resource_paths")
            if len(paths) != len(set(paths)):
                raise LinkValidationError("Duplicate review source resource paths")
            for path in paths:
                try:
                    _safe_name(path)
                except LiteratureError as exc:
                    raise LinkValidationError(str(exc)) from exc
                if path not in resources:
                    raise LinkValidationError("Missing review source resource")
            if "text_resource" in source and source["text_resource"] not in paths:
                raise LinkValidationError("Source text_resource is not in its resource closure")
            sources[source["id"]] = source
        for resource in _list(review.get("resources", []), "review.resources"):
            if not isinstance(resource, dict) or resource.get("path") not in resources:
                raise LinkValidationError("Missing declared review resource")
            if resource.get("sha256") != hashlib.sha256(resources[resource["path"]]).hexdigest():
                raise LinkValidationError("Review resource hash mismatch")
    papers = {row["id"]: row for row in source_document["papers"]}
    families = {row["id"]: row for row in source_document["datasets"]}
    relationships = {row["id"]: row for row in source_document["relationships"]}
    provider_datasets = {row["id"]: row for row in provider_document["datasets"]}
    records, questions, seen, question_ids = [], [], set(), set()
    global_resolution_identities = {}
    for review in reviews:
        family = review["family_id"]
        if family not in families:
            raise LinkValidationError("Review family is unknown")
        for row in _list(review.get("records"), "review.records"):
            if not isinstance(row, dict):
                raise LinkValidationError("Review record must be an object")
            rid = _text(row.get("relationship_id"), "relationship_id")
            if rid in seen:
                raise LinkValidationError(f"Duplicate reviewed relationship: {rid}")
            seen.add(rid)
            relation = relationships.get(rid)
            if relation is None:
                raise LinkValidationError("Review relationship is unknown")
            if row.get("paper_id") != relation["paper_id"]:
                raise LinkValidationError("Review paper disagrees with relationship")
            if family != relation["dataset_id"] or row.get("family_id", family) != family:
                raise LinkValidationError("Review family disagrees with relationship")
            paper = papers[row["paper_id"]]
            if row.get("use_decision") not in DECISIONS or row.get("review_basis") not in BASES:
                raise LinkValidationError("Invalid review decision or basis")
            if row.get("independently_human_validated") is not False:
                raise LinkValidationError(
                    "Review must explicitly retain independent human validation false"
                )
            _text(row.get("rationale"), "Review rationale")
            ref_ids = _refs(
                row, sources, "review", required=[ref["id"] for ref in paper["references"]]
            )
            evidence = []
            for item in _list(row.get("evidence"), "review.evidence"):
                checked, _ = _evidence(
                    item,
                    paper=paper,
                    sources=sources,
                    resources=resources,
                    provider_datasets=provider_datasets,
                )
                evidence.append(checked)
                ref_ids.add(checked["source_id"])
            if not evidence:
                raise LinkValidationError("Review requires publication evidence")
            if row["review_basis"] == "title_only" and any(
                item["field"] != "title" for item in evidence
            ):
                raise LinkValidationError("Title-only review contains other evidence")
            if row["review_basis"] == "publication_abstract" and any(
                item["field"] == "captured_text"
                and sources[item["source_id"]].get("type")
                not in {
                    "publication_metadata",
                    "publication_repository_metadata",
                    "bibliographic_metadata",
                }
                for item in evidence
            ):
                raise LinkValidationError("Abstract review contains full-text evidence")
            if row["review_basis"] == "publication_full_text" and not any(
                item["field"] == "captured_text" for item in evidence
            ):
                raise LinkValidationError("Full-text review lacks captured text evidence")
            resolved, resolution_ids = [], set()
            for resolution in _list(row.get("resolutions"), "review.resolutions"):
                if not isinstance(resolution, dict):
                    raise LinkValidationError("Resolution must be an object")
                identifier = _text(resolution.get("id"), "Resolution id")
                if identifier in resolution_ids:
                    raise LinkValidationError("Duplicate local resolution ID")
                resolution_ids.add(identifier)
                level = resolution.get("identity_level")
                if level not in LEVELS:
                    raise LinkValidationError("Invalid resolution identity_level")
                for key in ("provider", "title", "rationale"):
                    _text(resolution.get(key), f"Resolution {key}")
                dataset_id, native_id = resolution.get("dataset_id"), resolution.get("native_id")
                release = resolution.get("release")
                for key in ("dataset_id", "native_id", "release"):
                    if key not in resolution:
                        raise LinkValidationError(f"Resolution must explicitly declare {key}")
                    if resolution[key] is not None:
                        _text(resolution[key], f"Resolution {key}")
                dataset = provider_datasets.get(dataset_id)
                if level == "family":
                    if dataset_id != family or native_id is not None or release is not None:
                        raise LinkValidationError(
                            "Family resolution cannot assert a native dataset or release"
                        )
                elif dataset_id is not None:
                    if dataset is None:
                        raise LinkValidationError(
                            "Specific resolution dataset is absent from provider catalogue"
                        )
                    if (
                        dataset["native_id"] != native_id
                        or dataset["provider"] != resolution["provider"]
                    ):
                        raise LinkValidationError(
                            "Specific resolution native ID or provider disagrees with catalogue"
                        )
                elif level == "specific_dataset" or native_id is not None:
                    raise LinkValidationError(
                        "Specific dataset requires a known catalogue identity"
                    )
                if level == "specific_dataset" and release is not None:
                    raise LinkValidationError(
                        "A release assertion requires specific_release identity_level"
                    )
                identity = tuple(
                    resolution.get(key)
                    for key in ("provider", "dataset_id", "native_id", "release", "title")
                )
                if (
                    identifier in global_resolution_identities
                    and global_resolution_identities[identifier] != identity
                ):
                    raise LinkValidationError("Global resolution ID names conflicting identities")
                global_resolution_identities[identifier] = identity
                candidates = _list(
                    resolution.get("catalogue_candidates", []), "catalogue_candidates"
                )
                checked_candidates = []
                for candidate in candidates:
                    candidate_id = (
                        candidate.get("dataset_id", candidate.get("id"))
                        if isinstance(candidate, dict)
                        else candidate
                    )
                    if candidate_id not in provider_datasets:
                        raise LinkValidationError("Unknown resolution catalogue candidate")
                    candidate_dataset = provider_datasets[candidate_id]
                    candidate = (
                        candidate if isinstance(candidate, dict) else {"dataset_id": candidate_id}
                    )
                    if (
                        "native_id" in candidate
                        and candidate["native_id"] != candidate_dataset["native_id"]
                    ):
                        raise LinkValidationError(
                            "Catalogue candidate native ID disagrees with frozen metadata"
                        )
                    candidate_refs = {ref["id"] for ref in candidate_dataset["references"]}
                    if "references" in candidate:
                        candidate_refs |= _refs(candidate, sources, "catalogue candidate")
                    if "source_id" in candidate:
                        if candidate["source_id"] not in sources:
                            raise LinkValidationError("Unknown catalogue candidate source")
                        candidate_refs.add(candidate["source_id"])
                    candidate.update(
                        references=_references(candidate_refs, sources), benchmark_eligible=False
                    )
                    checked_candidates.append(candidate)
                rrefs = set(ref_ids)
                if "references" in resolution:
                    rrefs |= _refs(resolution, sources, "resolution")
                for candidate in checked_candidates:
                    rrefs |= {ref["id"] for ref in candidate["references"]}
                for candidate in _list(
                    resolution.get("uncatalogued_candidates", []), "uncatalogued_candidates"
                ):
                    if (
                        not isinstance(candidate, dict)
                        or candidate.get("benchmark_eligible") is not False
                        or candidate.get("in_frozen_provider_catalogue") is not False
                    ):
                        raise LinkValidationError(
                            "Uncatalogued candidate must remain a diagnostic without a positive label"
                        )
                    _text(candidate.get("title"), "Uncatalogued candidate title")
                    rrefs |= _refs(candidate, sources, "uncatalogued candidate")
                pub, provider, pub_text, provider_text = [], [], [], []
                for kind, target, texts in (
                    ("publication_evidence", pub, pub_text),
                    ("provider_evidence", provider, provider_text),
                ):
                    for item in _list(resolution.get(kind, []), kind):
                        checked, text = _evidence(
                            item,
                            paper=paper,
                            sources=sources,
                            resources=resources,
                            provider_datasets=provider_datasets,
                            provider=kind == "provider_evidence",
                            fallbacks=evidence,
                        )
                        target.append(checked)
                        texts.append(checked.get("excerpt", text))
                        rrefs.add(checked["source_id"])
                if level != "family" and (not pub or not provider):
                    raise LinkValidationError(
                        "Specific resolution requires publication and provider evidence"
                    )
                bound_title = dataset is not None and any(
                    proof["source_id"] in dataset["source_ids"]
                    and proof["verified_location"].get("resource_path")
                    == dataset.get("evidence_resource")
                    and (
                        not dataset.get("evidence_location")
                        or proof["verified_location"].get("json_pointer")
                        == dataset["evidence_location"]
                    )
                    and dataset["title"] in text
                    for proof, text in zip(provider, provider_text)
                )
                if (
                    native_id is not None
                    and not bound_title
                    and not any(native_id in text for text in provider_text)
                ):
                    raise LinkValidationError(
                        "Provider evidence does not identify the resolved native dataset"
                    )
                if level == "specific_release" and (
                    release is None
                    or not any(release in text for text in pub_text)
                    or not any(release in text for text in provider_text)
                ):
                    raise LinkValidationError(
                        f"Specific release is not literal in publication and provider evidence: {rid} / {identifier} ({release})"
                    )
                urls = _list(resolution.get("source_urls", []), "resolution.source_urls")
                if not urls or any(
                    url not in {sources[ref]["url"] for ref in rrefs} for url in urls
                ):
                    unknown = sorted(set(urls) - {sources[ref]["url"] for ref in rrefs})
                    raise LinkValidationError(
                        f"Resolution {identifier} source URLs lack verified references: {unknown}"
                    )
                resolution.update(
                    catalogue_candidates=checked_candidates,
                    publication_evidence=pub,
                    provider_evidence=provider,
                    references=_references(rrefs, sources),
                    benchmark_eligible=row["use_decision"] == "supported_use",
                    independently_human_validated=False,
                )
                resolved.append(resolution)
                ref_ids |= rrefs
            local = {resolution["id"]: resolution for resolution in resolved}
            for proposal in _list(row.get("questions"), "review.questions"):
                if not isinstance(proposal, dict):
                    raise LinkValidationError("Question proposal must be an object")
                identifier = _text(proposal.get("id"), "Question id")
                if identifier in question_ids:
                    raise LinkValidationError("Duplicate global question ID")
                question_ids.add(identifier)
                for key in ("text", "scenario_basis", "adaptation_notes", "question_kind"):
                    _text(proposal.get(key), f"Question {key}")
                ids = _list(proposal.get("resolution_ids"), "Question resolution_ids")
                if (
                    row["use_decision"] != "supported_use"
                    or not ids
                    or any(i not in local for i in ids)
                ):
                    raise LinkValidationError(
                        "Positive question requires supported use and local resolutions"
                    )
                if len(ids) != len(set(ids)):
                    raise LinkValidationError("Duplicate question resolution IDs")
                _constraints(proposal.get("constraints", {}))
                qrefs = set(ref_ids)
                if "references" in proposal:
                    qrefs |= _refs(proposal, sources, "question")
                proposal.update(
                    paper_id=paper["id"],
                    relationship_id=rid,
                    family_id=family,
                    references=_references(qrefs, sources),
                    independently_human_validated=False,
                )
                questions.append(proposal)
            row.update(
                family_id=family,
                evidence=evidence,
                resolutions=sorted(resolved, key=lambda item: item["id"]),
                questions=sorted(row["questions"], key=lambda item: item["id"]),
                references=_references(ref_ids, sources),
            )
            records.append(row)
    if seen != set(relationships):
        missing = sorted(set(relationships) - seen)
        raise LinkValidationError(
            f"Reviews do not cover every source relationship: {len(missing)} missing"
        )
    records.sort(key=lambda row: row["relationship_id"])
    unique = {}
    for row in records:
        for resolution in row["resolutions"]:
            identity = tuple(
                resolution.get(key)
                for key in ("provider", "dataset_id", "native_id", "release", "title")
            )
            if identity not in unique:
                unique[identity] = copy.deepcopy(resolution)
                unique[identity].update(resolution_ids=[], relationship_ids=[], paper_ids=[])
            result = unique[identity]
            result["resolution_ids"].append(resolution["id"])
            result["relationship_ids"].append(row["relationship_id"])
            result["paper_ids"].append(row["paper_id"])
            result["benchmark_eligible"] |= resolution["benchmark_eligible"]
            result["references"] = _references(
                [ref["id"] for ref in result["references"] + resolution["references"]], sources
            )
    resolutions = []
    for result in unique.values():
        for key in ("resolution_ids", "relationship_ids", "paper_ids"):
            result[key] = sorted(set(result[key]))
        result["id"] = result["resolution_ids"][0]
        resolutions.append(result)
    questions.sort(key=lambda row: row["id"])
    resolutions.sort(key=lambda row: row["id"])
    return {
        "schema_version": 1,
        "source_snapshot_id": source_snapshot_id,
        "validation_scope": "Complete relationship ledger, literal evidence and provider identity invariants; analytical use remains a review judgment.",
        "independently_human_validated": False,
        "records": records,
        "resolutions": resolutions,
        "questions": questions,
        "sources": sorted(sources.values(), key=lambda row: row["id"]),
        "counts": {
            "relationships": len(records),
            "papers": len({row["paper_id"] for row in records}),
            "use_decisions": dict(sorted(Counter(row["use_decision"] for row in records).items())),
            "identity_levels": dict(
                sorted(Counter(row["identity_level"] for row in resolutions).items())
            ),
            "unique_resolutions": len(resolutions),
            "questions": len(questions),
        },
    }
