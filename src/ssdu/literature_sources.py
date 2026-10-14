"""Pure, bounded adapters for publication–dataset evidence and bibliographic metadata.

These parsers never dereference a JSON-LD context or make network requests. Source
review is attributed to the source's curators; it is not independent SSDU review.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

VOCABULARY = "https://github.com/Coleridge-Initiative/adrf-onto/wiki/Vocabulary#"
RICH_CONTEXT = {
    "@language": "en",
    "@vocab": VOCABULARY,
    "cito": "http://purl.org/spar/cito/",
    "dct": "http://purl.org/dc/terms/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}
REFERENCE_FIELDS = ("id", "title", "url", "type", "published_at")


class LiteratureSourceError(ValueError):
    """A source cannot safely be interpreted using its declared format."""


def normalize_doi(value: str | None) -> str | None:
    """Return a lowercase DOI, accepting bare DOIs and doi.org URL wrappers."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise LiteratureSourceError("DOI must be a string or null")
    value = value.strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    elif re.match(r"https?://", value, re.IGNORECASE):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise LiteratureSourceError("Malformed DOI URL") from exc
        if (
            parsed.hostname not in {"doi.org", "dx.doi.org"}
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LiteratureSourceError("DOI URL must use doi.org without credentials or query")
        value = unquote(parsed.path.lstrip("/"))
    if (
        len(value) > 1000
        or not re.fullmatch(r"10\.\d{4,9}/[^\s<>\"'`\\?#%]+", value, re.ASCII)
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise LiteratureSourceError(f"Invalid DOI: {value!r}")
    return value.lower()


def _url(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 8000:
        raise LiteratureSourceError(f"{label} must be a nonempty HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate the optional port without rewriting the cited URL.
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not any(char.isspace() or ord(char) < 32 for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise LiteratureSourceError(f"{label} must be an HTTP(S) URL without credentials")
    return value


def _resource_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1000
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise LiteratureSourceError("resource_path must be a safe relative artifact key")
    return value


def _text(node: dict, field: str, *, required: bool = False) -> str | None:
    value = node.get(field)
    if value is None and not required:
        return None
    if (
        not isinstance(value, dict)
        or set(value) - {"@value", "@type", "@language"}
        or not isinstance(value.get("@value"), str)
        or not value["@value"].strip()
    ):
        raise LiteratureSourceError(f"{node.get('@id', 'node')}.{field} must be a literal")
    return value["@value"]


def _fragment(identifier: str, kind: str) -> str:
    prefix = VOCABULARY + kind + "-"
    if not isinstance(identifier, str) or not identifier.startswith(prefix):
        raise LiteratureSourceError(f"Expected a Rich Context {kind} identifier")
    fragment = identifier[len(VOCABULARY) :]
    if not re.fullmatch(kind + r"-[A-Za-z0-9_.-]+", fragment):
        raise LiteratureSourceError(f"Invalid Rich Context identifier: {identifier}")
    return fragment


def _id(identifier: str, kind: str) -> str:
    return "rich-context:" + _fragment(identifier, kind)


def _reference(source: dict) -> dict:
    return {field: source.get(field) for field in REFERENCE_FIELDS}


def _doi(node: dict) -> str | None:
    value = _text(node, "dct:identifier")
    if value is None:
        return None
    if value.lower().startswith(
        (
            "10.",
            "doi:",
            "https://doi.org/",
            "http://doi.org/",
            "http://dx.doi.org/",
            "https://dx.doi.org/",
        )
    ):
        try:
            return normalize_doi(value)
        except LiteratureSourceError:
            return None
    return None  # A non-DOI identifier is not converted into a DOI.


def _raw_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > 20_000_000:
        raise LiteratureSourceError("Rich Context input must be bytes, at most 20 MB")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LiteratureSourceError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise LiteratureSourceError(f"Invalid JSON constant: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LiteratureSourceError(f"Cannot parse Rich Context JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("@context") != RICH_CONTEXT:
        raise LiteratureSourceError(
            "Unsupported Rich Context JSON-LD context; remote contexts are disabled"
        )
    if not isinstance(value.get("@graph"), list) or len(value["@graph"]) > 50_000:
        raise LiteratureSourceError("Rich Context @graph must be a list of at most 50000 nodes")
    return value


def parse_rich_context(
    raw: bytes,
    *,
    source_url: str,
    revision: str,
    publication_ids: list[str] | None = None,
    max_papers: int = 20,
    resource_path: str = "rich-context.jsonld",
) -> dict:
    """Normalize selected publications and their complete dataset/provider linkage.

    Selection is deterministic and accepts native fragments, normalized IDs, or
    full vocabulary IDs. No geography, version, or release date is inferred.
    """
    _url(source_url, "source_url")
    _resource_path(resource_path)
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", revision):
        raise LiteratureSourceError("revision must be a bounded immutable revision identifier")
    if type(max_papers) is not int or not 1 <= max_papers <= 100:
        raise LiteratureSourceError("max_papers must be between 1 and 100")
    parsed = _raw_json(raw)
    nodes, positions = {}, {}
    for position, node in enumerate(parsed["@graph"]):
        if not isinstance(node, dict) or not isinstance(node.get("@id"), str):
            raise LiteratureSourceError("Every graph node must have a string @id")
        if node["@id"] in nodes:
            raise LiteratureSourceError(f"Duplicate graph identifier: {node['@id']}")
        nodes[node["@id"]] = node
        positions[node["@id"]] = position
    publications = {
        key: node for key, node in nodes.items() if node.get("@type") == "ResearchPublication"
    }
    if publication_ids is None:
        selected = sorted(publications)[:max_papers]
    else:
        if not isinstance(publication_ids, list) or any(
            not isinstance(key, str) for key in publication_ids
        ):
            raise LiteratureSourceError("publication_ids must be a list of identifiers")
        selected = []
        for key in publication_ids:
            key = key.removeprefix("rich-context:")
            key = key if key.startswith(VOCABULARY) else VOCABULARY + key
            _fragment(key, "publication")
            if key not in publications:
                raise LiteratureSourceError(f"Unknown selected publication: {key}")
            selected.append(key)
        if len(set(selected)) != len(selected):
            raise LiteratureSourceError("Duplicate selected publication")
        if not selected or len(selected) > max_papers:
            raise LiteratureSourceError(
                "Selected publications must be nonempty and within max_papers"
            )
        selected.sort()
    if not selected:
        raise LiteratureSourceError("Rich Context corpus contains no publications")
    result = _rich_context_records(
        nodes,
        positions,
        selected,
        source_url=source_url,
        revision=revision,
        resource_path=resource_path,
    )
    result["selection"] = {
        "method": "explicit_ids" if publication_ids is not None else "sorted_ids",
        "publication_ids": selected,
        "max_papers": max_papers,
    }
    return result


def _rich_context_source(source_url: str, revision: str, resource_path: str) -> dict:
    return {
        "id": "rich-context:" + revision,
        "title": "Coleridge Rich Context publication–dataset corpus",
        "url": source_url,
        "type": "literature_corpus",
        "published_at": None,
        "revision": revision,
        "resource_paths": [resource_path],
    }


def _rich_context_row_url(
    row: dict,
    value: str,
    *,
    normalize_outer_whitespace: bool = False,
) -> None:
    original = value
    if normalize_outer_whitespace:
        value = value.strip()
    row["url"] = _url(value, f"resource {row['id']} URL")
    if value != original:
        row["metadata_issues"] = [
            {
                "field": "foaf:page",
                "source_value": original,
                "normalized_value": value,
                "method": "outer_whitespace_trim",
            }
        ]


def _rich_context_provider(
    provider_key: str,
    nodes: dict,
    corpus_source: dict,
    *,
    normalize_outer_whitespace: bool = False,
) -> dict:
    provider_id = _id(provider_key, "provider")
    provider = nodes.get(provider_key)
    if provider is None or provider.get("@type") != "Provider":
        raise LiteratureSourceError(
            f"Broken referenced provider (missing or ambiguous): {provider_key}"
        )
    row = {
        "id": provider_id,
        "title": _text(provider, "dct:title", required=True),
        "source_ids": [corpus_source["id"]],
        "references": [_reference(corpus_source)],
    }
    _rich_context_row_url(
        row,
        _text(provider, "foaf:page") or provider_key,
        normalize_outer_whitespace=normalize_outer_whitespace,
    )
    return row


def _rich_context_dataset(
    dataset_key: str,
    nodes: dict,
    corpus_source: dict,
    *,
    normalize_outer_whitespace: bool = False,
) -> tuple[dict, dict]:
    dataset_id = _id(dataset_key, "dataset")
    dataset = nodes.get(dataset_key)
    if dataset is None or dataset.get("@type") != "Dataset":
        raise LiteratureSourceError(
            f"Broken referenced dataset (missing or ambiguous): {dataset_key}"
        )
    publisher = dataset.get("dct:publisher")
    if isinstance(publisher, dict) and set(publisher) == {"@id"}:
        provider_key = publisher["@id"]
    else:
        provider_fragment = _text(dataset, "dct:publisher", required=True)
        provider_key = VOCABULARY + provider_fragment
    provider = _rich_context_provider(
        provider_key,
        nodes,
        corpus_source,
        normalize_outer_whitespace=normalize_outer_whitespace,
    )
    row = {
        "id": dataset_id,
        "title": _text(dataset, "dct:title", required=True),
        "provider": provider["title"],
        "provider_id": provider["id"],
        "catalogue_entry_kind": "source_catalogue_entry",
        "notes": "Source entity may describe a programme or data family; release precision is not guaranteed.",
        "native_id": _fragment(dataset_key, "dataset"),
        "description": _text(dataset, "dct:description") or "",
        "doi": _doi(dataset),
        "version": None,
        "coverage": {},
        "source_ids": [corpus_source["id"]],
        "references": [_reference(corpus_source)],
    }
    _rich_context_row_url(
        row,
        _text(dataset, "foaf:page") or dataset_key,
        normalize_outer_whitespace=normalize_outer_whitespace,
    )
    return row, provider


def _rich_context_publication(
    publication_key: str,
    nodes: dict,
    positions: dict,
    corpus_source: dict,
    *,
    allow_unlinked: bool = False,
    normalize_outer_whitespace: bool = False,
) -> dict:
    """Build one complete closure before exposing any of its records."""
    corpus_reference = _reference(corpus_source)
    source_url = corpus_source["url"]
    resource_path = corpus_source["resource_paths"][0]
    node = nodes[publication_key]
    paper_id = _id(publication_key, "publication")
    title = _text(node, "dct:title", required=True)
    doi = _doi(node)
    paper_url = _text(node, "foaf:page") or (f"https://doi.org/{doi}" if doi else publication_key)
    normalized_paper = {"id": paper_id}
    _rich_context_row_url(
        normalized_paper, paper_url, normalize_outer_whitespace=normalize_outer_whitespace
    )
    paper_url = normalized_paper["url"]
    citation = {
        "id": "reference:" + paper_id,
        "title": title,
        "url": paper_url,
        "type": "research_publication",
        "published_at": None,
        "resource_paths": [resource_path],
        "bibliographic_metadata_from": corpus_source["id"],
    }
    paper_reference = _reference(citation)
    paper = {
        "id": paper_id,
        "title": title,
        "doi": doi,
        "url": paper_url,
        "abstract": None,
        "source_identifier": _text(node, "dct:identifier"),
        "identifier_issue": "Source identifier is not a valid DOI"
        if _text(node, "dct:identifier") and doi is None
        else None,
        "published_at": None,
        "source_ids": [corpus_source["id"], citation["id"]],
        "references": [corpus_reference, paper_reference],
    }
    if "metadata_issues" in normalized_paper:
        paper["metadata_issues"] = normalized_paper["metadata_issues"]
    datasets, providers, relationships = {}, {}, []
    links = node.get("cito:citesAsDataSource")
    if allow_unlinked and (links is None or links == []):
        links = []
    else:
        links = links if isinstance(links, list) else [links]
        if not links or len(links) > 1000:
            raise LiteratureSourceError(f"Invalid dataset links in {paper_id}")
    seen_links = set()
    for link_number, link in enumerate(links):
        if not isinstance(link, dict) or set(link) != {"@id"}:
            raise LiteratureSourceError(f"Dataset link must contain only @id in {paper_id}")
        dataset_key = link["@id"]
        dataset_id = _id(dataset_key, "dataset")
        if dataset_key in seen_links:
            raise LiteratureSourceError(f"Duplicate dataset link in {paper_id}")
        seen_links.add(dataset_key)
        dataset, provider = _rich_context_dataset(
            dataset_key,
            nodes,
            corpus_source,
            normalize_outer_whitespace=normalize_outer_whitespace,
        )
        datasets[dataset_id] = dataset
        providers[provider["id"]] = provider
        location = f"/@graph/{positions[publication_key]}/cito:citesAsDataSource"
        if isinstance(node.get("cito:citesAsDataSource"), list):
            location += f"/{link_number}"
        relationships.append(
            {
                "id": f"{paper_id}:uses:{_fragment(dataset_key, 'dataset')}",
                "paper_id": paper_id,
                "dataset_id": dataset_id,
                "relationship_type": "source_asserted_use",
                "asserted_by": "Rich Context curators",
                "evidence": {
                    "source_id": corpus_source["id"],
                    "location": location,
                    "summary": "Rich Context asserts this publication cites the dataset as a data source; repository documentation attributes annotation verification to domain experts.",
                    "url": source_url,
                    "predicate": "cito:citesAsDataSource",
                },
                "dataset_version": None,
                "review": {
                    "status": "source_reviewed",
                    "reviewer": "Rich Context curator team (as reported by repository)",
                    "independently_human_validated": False,
                },
                "references": [corpus_reference, paper_reference],
            }
        )
    return {
        "sources": [citation],
        "papers": [paper],
        "datasets": list(datasets.values()),
        "providers": list(providers.values()),
        "relationships": relationships,
    }


def _rich_context_records(
    nodes: dict,
    positions: dict,
    selected: list[str],
    *,
    source_url: str,
    revision: str,
    resource_path: str,
) -> dict:
    corpus_source = _rich_context_source(source_url, revision, resource_path)
    sources, papers, datasets, providers, relationships = [corpus_source], [], {}, {}, []
    for publication_key in selected:
        records = _rich_context_publication(publication_key, nodes, positions, corpus_source)
        sources.extend(records["sources"])
        papers.extend(records["papers"])
        datasets.update((row["id"], row) for row in records["datasets"])
        providers.update((row["id"], row) for row in records["providers"])
        relationships.extend(records["relationships"])
    return {
        "schema_version": 1,
        "sources": sources,
        "papers": papers,
        "datasets": [datasets[key] for key in sorted(datasets)],
        "providers": [providers[key] for key in sorted(providers)],
        "relationships": relationships,
    }


def parse_rich_context_full(
    raw: bytes,
    *,
    source_url: str,
    revision: str,
    resource_path: str = "rich-context.jsonld",
) -> dict:
    """Import every valid paper/dataset/provider, retaining rejects and raw pointers.

    A malformed publication or its referenced dataset/provider closure is
    quarantined as a whole. Unlinked valid publications are retained. Duplicate
    identifiers are ambiguous, so neither duplicate is chosen. Structural JSON
    and context errors fail the entire import. Outer URL whitespace is trimmed
    with the original value and operation retained. Geography and release precision
    remain unknown unless supplied by another independently attributed source.
    """
    _url(source_url, "source_url")
    _resource_path(resource_path)
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", revision):
        raise LiteratureSourceError("revision must be a bounded immutable revision identifier")
    parsed = _raw_json(raw)
    graph = parsed["@graph"]
    nodes, positions, duplicates = {}, {}, set()
    malformed_graph_nodes = 0
    publication_rows = []
    dataset_rows = []
    provider_rows = []
    for position, node in enumerate(graph):
        if isinstance(node, dict):
            if node.get("@type") == "ResearchPublication":
                publication_rows.append((position, node))
            elif node.get("@type") == "Dataset":
                dataset_rows.append((position, node))
            elif node.get("@type") == "Provider":
                provider_rows.append((position, node))
        if not isinstance(node, dict) or not isinstance(node.get("@id"), str):
            malformed_graph_nodes += 1
            continue
        identifier = node["@id"]
        if identifier in nodes:
            duplicates.add(identifier)
        else:
            nodes[identifier] = node
            positions[identifier] = position
    for identifier in duplicates:
        del nodes[identifier]
        del positions[identifier]

    corpus_source = _rich_context_source(source_url, revision, resource_path)
    corpus_reference = _reference(corpus_source)
    sources, papers, datasets, providers, relationships = [corpus_source], [], {}, {}, []
    quarantine = []

    def pointer(position, node):
        return {
            "source_id": corpus_source["id"],
            "resource_path": resource_path,
            "location": f"/@graph/{position}",
            "source_record_id": node.get("@id") if isinstance(node.get("@id"), str) else None,
        }

    def dependency_locations(node):
        """Retain graph pointers for the closure inspected before a rejection."""
        locations = {}
        links = node.get("cito:citesAsDataSource")
        links = links if isinstance(links, list) else [links]
        if node.get("@type") == "Dataset":
            links = [{"@id": node.get("@id")}]
        for link in links:
            key = link.get("@id") if isinstance(link, dict) else None
            if not isinstance(key, str) or key not in nodes:
                continue
            locations[key] = pointer(positions[key], nodes[key])
            publisher = nodes[key].get("dct:publisher")
            provider_key = None
            if isinstance(publisher, dict):
                if isinstance(publisher.get("@id"), str):
                    provider_key = publisher["@id"]
                elif isinstance(publisher.get("@value"), str):
                    provider_key = VOCABULARY + publisher["@value"]
            if provider_key in nodes:
                locations[provider_key] = pointer(positions[provider_key], nodes[provider_key])
        return [locations[key] for key in sorted(locations)]

    def reject(kind, position, node, exc):
        quarantine.append(
            {
                "id": f"rich-context:rejected-{kind}:graph-{position}",
                "kind": kind,
                "status": "rejected",
                **pointer(position, node),
                "reason": str(exc),
                "dependency_locations": dependency_locations(node),
                "source_ids": [corpus_source["id"]],
                "references": [corpus_reference],
            }
        )

    for kind, entity_rows, target in (
        ("provider", provider_rows, providers),
        ("dataset", dataset_rows, datasets),
    ):
        for position, node in sorted(
            entity_rows, key=lambda item: (str(item[1].get("@id", "")), item[0])
        ):
            identifier = node.get("@id")
            try:
                _fragment(identifier, kind)
                if identifier in duplicates:
                    raise LiteratureSourceError(f"Duplicate graph identifier: {identifier}")
                if kind == "provider":
                    row = _rich_context_provider(
                        identifier, nodes, corpus_source, normalize_outer_whitespace=True
                    )
                else:
                    row, _ = _rich_context_dataset(
                        identifier, nodes, corpus_source, normalize_outer_whitespace=True
                    )
            except LiteratureSourceError as exc:
                reject(kind, position, node, exc)
                continue
            row["source_locations"] = [pointer(position, node)]
            target[row["id"]] = row

    for position, node in sorted(
        publication_rows, key=lambda item: (str(item[1].get("@id", "")), item[0])
    ):
        publication_key = node.get("@id")
        try:
            _fragment(publication_key, "publication")
            if publication_key in duplicates:
                raise LiteratureSourceError(f"Duplicate graph identifier: {publication_key}")
            records = _rich_context_publication(
                publication_key,
                nodes,
                positions,
                corpus_source,
                allow_unlinked=True,
                normalize_outer_whitespace=True,
            )
        except LiteratureSourceError as exc:
            reject("publication", position, node, exc)
            continue
        for key in ("papers", "datasets", "providers"):
            for row in records[key]:
                original_id = VOCABULARY + row["id"].removeprefix("rich-context:")
                row["source_locations"] = [pointer(positions[original_id], nodes[original_id])]
        sources.extend(records["sources"])
        papers.extend(records["papers"])
        datasets.update((row["id"], row) for row in records["datasets"])
        providers.update((row["id"], row) for row in records["providers"])
        relationships.extend(records["relationships"])

    referenced_dataset_ids = {
        VOCABULARY + row["dataset_id"].removeprefix("rich-context:") for row in relationships
    }
    unused_dataset_nodes = [
        {
            "id": f"rich-context:unused-dataset:graph-{position}",
            "kind": "dataset",
            "status": "unused_by_imported_publications",
            **pointer(position, node),
            "source_ids": [corpus_source["id"]],
            "references": [corpus_reference],
        }
        for position, node in dataset_rows
        if not isinstance(node.get("@id"), str) or node["@id"] not in referenced_dataset_ids
    ]
    linked_papers = {row["paper_id"] for row in relationships}
    referenced_providers = {row["provider_id"] for row in datasets.values()}
    rejected_counts = {
        kind: sum(row["kind"] == kind for row in quarantine)
        for kind in ("publication", "dataset", "provider")
    }
    return {
        "schema_version": 1,
        "sources": sources,
        "papers": papers,
        "datasets": [datasets[key] for key in sorted(datasets)],
        "providers": [providers[key] for key in sorted(providers)],
        "relationships": relationships,
        "quarantine": sorted(quarantine, key=lambda row: row["location"]),
        "unused_dataset_nodes": unused_dataset_nodes,
        "coverage_report": {
            "source_graph_nodes": len(graph),
            "malformed_graph_nodes": malformed_graph_nodes,
            "duplicate_graph_identifiers": len(duplicates),
            "source_publications": len(publication_rows),
            "imported_publications": len(papers),
            "rejected_publications": rejected_counts["publication"],
            "linked_publications": len(linked_papers),
            "unlinked_publications": len(papers) - len(linked_papers),
            "publications_with_doi": sum(row["doi"] is not None for row in papers),
            "publications_without_doi": sum(row["doi"] is None for row in papers),
            "publications_with_identifier_issue": sum(
                row["identifier_issue"] is not None for row in papers
            ),
            "source_dataset_nodes": len(dataset_rows),
            "imported_dataset_records": len(datasets),
            "rejected_dataset_records": rejected_counts["dataset"],
            "unused_dataset_nodes": len(unused_dataset_nodes),
            "source_provider_nodes": len(provider_rows),
            "imported_provider_records": len(providers),
            "rejected_provider_records": rejected_counts["provider"],
            "providers_unreferenced_by_imported_datasets": len(providers)
            - len(referenced_providers),
            "url_outer_whitespace_normalizations": sum(
                bool(row.get("metadata_issues"))
                for row in papers + list(datasets.values()) + list(providers.values())
            ),
            "relationships": len(relationships),
            "uk_relevance": "unassessed",
            "independent_human_review": "pending",
        },
        "selection": {
            "method": "all_publications",
            "publication_ids": [
                node["@id"] for _, node in publication_rows if isinstance(node.get("@id"), str)
            ],
        },
    }


def reconstruct_abstract(index: dict | None, *, max_tokens: int = 20_000) -> str | None:
    """Decode OpenAlex positions, rejecting duplicates, gaps, and excessive inputs."""
    if index is None or index == {}:
        return None
    if not isinstance(index, dict) or len(index) > max_tokens:
        raise LiteratureSourceError("Abstract inverted index must be a bounded object or null")
    positions = {}
    characters = 0
    for token, locations in index.items():
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(locations, list)
            or not locations
        ):
            raise LiteratureSourceError("Malformed abstract token or positions")
        for position in locations:
            if type(position) is not int or not 0 <= position < max_tokens or position in positions:
                raise LiteratureSourceError("Abstract positions must be unique bounded integers")
            positions[position] = token
            characters += len(token)
            if characters > 1_000_000:
                raise LiteratureSourceError("Abstract text exceeds 1 MB")
    if set(positions) != set(range(len(positions))):
        raise LiteratureSourceError("Abstract positions must be contiguous from zero")
    return " ".join(positions[position] for position in range(len(positions)))


def enrich_openalex(
    document: dict,
    works: list[dict],
    *,
    source_url: str,
    resource_path: str = "openalex.json",
    captured_at: str | None = None,
) -> dict:
    """Enrich exact DOI matches, retaining existing metadata and field provenance.

    Known titles/abstracts/dates are retained. Conflicting upstream values remain
    visible in metadata_provenance, with the fields that were actually applied.
    """
    _url(source_url, "source_url")
    _resource_path(resource_path)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise LiteratureSourceError("Expected a schema_version 1 literature document")
    if (
        not isinstance(works, list)
        or len(works) > 1000
        or any(not isinstance(work, dict) for work in works)
    ):
        raise LiteratureSourceError("works must be a list of at most 1000 OpenAlex records")
    result = copy.deepcopy(document)
    source_id = "openalex:" + hashlib.sha256(source_url.encode()).hexdigest()[:20]
    source = {
        "id": source_id,
        "title": "OpenAlex work metadata",
        "url": source_url,
        "type": "bibliographic_metadata",
        "published_at": None,
        "captured_at": captured_at,
        "resource_paths": [resource_path],
        "metadata_licence": "CC0",
        "licence_url": "https://help.openalex.org/data/how-its-built/",
        "abstract_rights": "Upstream article rights may apply",
    }
    by_doi = {}
    for work in works:
        doi = normalize_doi(work.get("doi"))
        if doi is None:
            continue
        if doi in by_doi:
            raise LiteratureSourceError(f"Duplicate OpenAlex DOI: {doi}")
        by_doi[doi] = work
    matched = False
    for paper in result.get("papers", []):
        work = by_doi.get(normalize_doi(paper.get("doi")))
        if work is None:
            continue
        matched = True
        record_id = work.get("id")
        if not isinstance(record_id, str) or not re.fullmatch(
            r"https://openalex\.org/W\d+", record_id
        ):
            raise LiteratureSourceError("Matched OpenAlex work requires an OpenAlex work ID")
        title = work.get("title", work.get("display_name"))
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise LiteratureSourceError("OpenAlex title must be a nonempty string or null")
        published_at = work.get("publication_date")
        if published_at is not None:
            try:
                if (
                    not isinstance(published_at, str)
                    or date.fromisoformat(published_at).isoformat() != published_at
                ):
                    raise ValueError
            except ValueError as exc:
                raise LiteratureSourceError(
                    "OpenAlex publication_date must be ISO YYYY-MM-DD"
                ) from exc
        values = {
            "title": title,
            "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
            "published_at": published_at,
        }
        previous = paper.setdefault("metadata_provenance", [])
        prior = next(
            (
                item
                for item in previous
                if item.get("source_id") == source_id and item.get("record_id") == record_id
            ),
            None,
        )
        if prior is not None:
            if prior.get("values") != values:
                raise LiteratureSourceError(
                    "Conflicting OpenAlex work metadata for an existing capture"
                )
            continue
        applied, conflicts = [], []
        for field, value in values.items():
            if value is None:
                continue
            if paper.get(field) is None:
                paper[field] = value
                applied.append(field)
            elif paper[field] != value:
                conflicts.append(field)
        provenance = {
            "source_id": source_id,
            "record_id": record_id,
            "matched_by": "doi",
            "doi": normalize_doi(paper.get("doi")),
            "values": values,
            "applied_fields": applied,
            "conflicting_fields": conflicts,
        }
        if provenance not in previous:
            previous.append(provenance)
        if source_id not in paper["source_ids"]:
            paper["source_ids"].append(source_id)
            paper["references"].append(_reference(source))
        paper["abstract_rights"] = "Upstream article rights may apply"
    if matched:
        previous = next((item for item in result["sources"] if item["id"] == source_id), None)
        if previous is not None and previous != source:
            raise LiteratureSourceError("Conflicting OpenAlex source capture for the same URL")
        if previous is None:
            result["sources"].append(source)
    return result
