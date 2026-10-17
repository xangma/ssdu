"""Bounded StatsWales v2 retrieval, preserving the API's exact cell values.

API contracts were inspected via https://api.stats.gov.wales/v2/docs and
https://github.com/jamie-ralph/statswalesr (R/statswales_get_dataset.R,
R/statswales_get_query.R and R/statswales_get_filters.R). This is an independent
Python implementation. Live checks found that internal factTableColumn filter
keys work, human column names fail, and POST /data omits totalLines; GET /query
supplies the count and revision. Only the observed working 'formatted' option
is used: Notes, padded values and internal sort columns are intentionally kept.
"""

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import pandas as pd

from ssdu.http import HttpClient
from ssdu.metadata_mapping import mapping_metadata, normalized_metadata_fields, plain_text
from ssdu.models import Acquisition, Request, RetrievalError

BASE = "https://api.stats.gov.wales/v2"
DEFAULT_ID = "40ddfa33-9ba8-4278-ac27-1f1b0bc5fc0f"


def default_request() -> Request:
    """A catalogue-discovered, 27-row income-poverty example (three-year average)."""
    return Request(
        "statswales",
        DEFAULT_ID,
        {"filters": [{"YearID": ["3Y2025-03-31"]}], "lang": "en-gb"},
        page_size=10,
        max_rows=100,
    )


def search(query: str, client: HttpClient) -> list[dict]:
    """Return at most 25 catalogue matches, without claiming exhaustive discovery."""
    params = {"lang": "en-gb", "page_number": 1, "page_size": 25}
    path = BASE
    if query.strip():
        params.update({"keywords": query, "mode": "basic"})
        path += "/search"
    response = client.get(path, params=params).json()
    return _catalogue_rows(response, max_size=25)


def _catalogue_rows(response, *, max_size):
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise RetrievalError("StatsWales catalogue response has no data list")
    if len(response["data"]) > max_size:
        raise RetrievalError("StatsWales catalogue page exceeds requested page_size")
    result = []
    for entry in response["data"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("title"):
            raise RetrievalError("Malformed StatsWales catalogue entry")
        result.append(
            {"id": _dataset_id(entry["id"]), "title": entry["title"], "provider": "statswales"}
        )
    return result


def _window(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("offset must be an integer from 0 to 1000000")


def _dataset_id(dataset_id):
    try:
        return str(UUID(dataset_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RetrievalError("StatsWales dataset_id must be a UUID") from exc


def _catalogue_counters(response, page, size):
    # The provider has used different counter names. Preserve unknown totals
    # and return a continuation on full pages instead of silently stopping.
    native = response.get("pagination", response)
    if not isinstance(native, dict):
        raise RetrievalError("Malformed StatsWales catalogue pagination")
    counters = {}
    for name, keys in {
        "total": ("count", "total_count", "totalCount", "total_results", "totalResults", "total"),
        "pages": ("total_pages", "totalPages"),
        "page": ("page_number", "pageNumber", "current_page", "currentPage"),
        "size": ("page_size", "pageSize", "per_page", "perPage"),
    }.items():
        values = [native[key] for key in keys if key in native]
        if any(type(value) is not int or value < 0 for value in values):
            raise RetrievalError("StatsWales catalogue count is invalid")
        if len(set(values)) > 1:
            raise RetrievalError("StatsWales catalogue count fields disagree")
        if values:
            counters[name] = values[0]
    if counters.get("page", page) != page or counters.get("size", size) != size:
        raise RetrievalError("StatsWales catalogue pagination does not match request")
    if (
        "pages" in counters
        and "total" in counters
        and counters["pages"] != (counters["total"] + size - 1) // size
    ):
        raise RetrievalError("StatsWales catalogue total and page count disagree")
    return counters


def list_datasets(query: str, client: HttpClient, *, limit=20, offset=0) -> dict:
    """Browse provider metadata with explicit continuation, never observations."""
    _window(limit, offset)
    if not isinstance(query, str):
        raise TypeError("StatsWales query must be a string")
    params = {"lang": "en-gb", "page_size": limit}
    path = BASE
    if query.strip():
        params.update({"keywords": query.strip(), "mode": "basic"})
        path += "/search"
    page = offset // limit + 1
    skip = offset % limit
    rows, total, pages, seen = [], None, None, set()
    exhausted = False
    while len(rows) < limit:
        response = client.get(path, params={**params, "page_number": page}).json()
        parsed = _catalogue_rows(response, max_size=limit)
        counters = _catalogue_counters(response, page, limit)
        count = counters.get("total")
        if total is not None and count is not None and count != total:
            raise RetrievalError("StatsWales catalogue count changed across pages")
        total = count if count is not None else total
        if pages is not None and counters.get("pages", pages) != pages:
            raise RetrievalError("StatsWales catalogue page count changed across pages")
        pages = counters.get("pages", pages)
        batch = response["data"]
        if len(batch) > limit:
            raise RetrievalError("StatsWales catalogue page exceeds requested page_size")
        if total is not None and len(batch) != min(limit, max(0, total - (page - 1) * limit)):
            raise RetrievalError("StatsWales catalogue page disagrees with its declared count")
        for entry in parsed:
            identity = entry["id"]
            if identity in seen:
                raise RetrievalError("StatsWales catalogue repeats a dataset across pages")
            seen.add(identity)
        rows.extend(parsed[skip : skip + limit - len(rows)])
        skip = 0
        exhausted = (
            len(batch) < limit
            or (total is not None and page * limit >= total)
            or (pages is not None and page >= pages)
        )
        if exhausted:
            break
        page += 1
    next_offset = offset + len(rows) if rows and not exhausted else None
    if total is not None:
        next_offset = offset + len(rows) if offset + len(rows) < total else None
    return {"items": rows, "total": total, "next_offset": next_offset}


def catalogue_inventory(query: str, client: HttpClient, *, max_records=1000) -> dict:
    """Reconcile a bounded inventory without offset-page ordering assumptions.

    The API's offset pages can repeat or omit IDs at tied publication times.
    A single bounded response avoids that boundary when the catalogue fits. A
    larger catalogue remains explicitly incomplete; it is never resampled until
    a desired count is reached or presented as an atomic provider snapshot.
    """
    _window(max_records, 0)
    if not isinstance(query, str):
        raise TypeError("StatsWales query must be a string")
    params = {"lang": "en-gb", "page_size": max_records, "page_number": 1}
    path = BASE
    if query.strip():
        params.update({"keywords": query.strip(), "mode": "basic"})
        path += "/search"
    response = client.get(path, params=params).json()
    rows = _catalogue_rows(response, max_size=max_records)
    counters = _catalogue_counters(response, 1, max_records)
    total = counters.get("total")
    distinct = {row["id"]: row for row in rows}
    duplicate_count = len(rows) - len(distinct)
    expected = min(total, max_records) if total is not None else None
    count_matches = expected is None or len(rows) == expected
    exhausted = (total is not None and len(rows) >= total) or (
        total is None and len(rows) < max_records
    )
    errors = []
    if duplicate_count:
        errors.append({"stage": "catalogue", "code": "duplicate_ids"})
    if not count_matches:
        errors.append({"stage": "catalogue", "code": "declared_count_mismatch"})
    complete = exhausted and not errors
    return {
        "items": list(distinct.values()),
        "total": total,
        "next_offset": None if complete else len(rows),
        "observed_distinct": len(distinct),
        "duplicate_count": duplicate_count,
        "errors": errors,
        "inventory_complete": complete,
        "reconciliation": {
            "method": "bounded_single_response",
            "max_records": max_records,
            "atomic_provider_snapshot": False,
        },
    }


def _metadata(dataset_id, client, *, lang="en-gb"):
    dataset_id = _dataset_id(dataset_id)
    source = BASE + "/" + dataset_id
    before = client.get(source, params={"lang": lang}).json()
    dimensions = client.get(source + "/filters", params={"lang": lang}).json()
    if (
        not isinstance(before, dict)
        or before.get("id", dataset_id) != dataset_id
        or not isinstance(dimensions, list)
        or any(not isinstance(dim, dict) for dim in dimensions)
    ):
        raise RetrievalError("Malformed StatsWales metadata or dimensions")
    return before, dimensions


def _title(metadata, dataset_id, lang="en-gb"):
    revision = metadata.get("published_revision", {})
    if not isinstance(revision, dict):
        raise RetrievalError("Malformed StatsWales published revision")
    translated = revision.get("metadata", [])
    if not isinstance(translated, list):
        return dataset_id
    language = "en-gb" if lang == "en" else "cy-gb" if lang == "cy" else lang
    for entry in translated:
        if isinstance(entry, dict) and str(entry.get("language", "")).lower() == language:
            return entry.get("title") or dataset_id
    return dataset_id


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _http_url(value):
    value = _text(value)
    if value is None:
        return None
    parsed = urlsplit(value)
    return (
        value
        if parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not (parsed.username or parsed.password)
        else None
    )


def _discovery_fields(metadata):
    """Map declared catalogue identities and relationships, retaining native prose."""
    revision = metadata.get("published_revision", {})
    publishers, subjects, related, evidence = [], [], [], []
    incomplete = set()

    def add(field, rows, item, path):
        if item and item not in rows:
            rows.append(item)
            evidence.append({"field": field, "source_path": path, "method": "provider_assertion"})

    publisher = metadata.get("publisher")
    if isinstance(publisher, dict):
        for key, role in (("organisation", "publisher"), ("group", "publishing_group")):
            native = publisher.get(key)
            if not isinstance(native, dict):
                continue
            item = {key: value for key in ("id", "name") if (value := _text(native.get(key)))}
            if item:
                add("publishers", publishers, {**item, "role": role}, f"/publisher/{key}")
    elif publisher is not None:
        incomplete.add("publishers")

    providers = revision.get("providers", [])
    if not isinstance(providers, list):
        if providers is not None:
            incomplete.update({"publishers", "related"})
        providers = []
    # Prefer the explicitly English translation for a repeated identity; preserve
    # every alternative assertion in source_metadata rather than inventing names.
    providers = sorted(
        enumerate(providers),
        key=lambda pair: (
            str(pair[1].get("language", "")).lower() not in {"en", "en-gb"}
            if isinstance(pair[1], dict)
            else True,
            pair[0],
        ),
    )
    seen_providers, seen_sources = set(), set()
    for index, native in providers:
        if not isinstance(native, dict):
            incomplete.add("publishers")
            continue
        identity, name = _text(native.get("provider_id")), _text(native.get("provider_name"))
        token = identity or name
        if token and token not in seen_providers:
            seen_providers.add(token)
            item = {key: value for key, value in (("id", identity), ("name", name)) if value}
            add(
                "publishers",
                publishers,
                {**item, "role": "data_provider"},
                f"/published_revision/providers/{index}",
            )
        source_id, title = _text(native.get("source_id")), _text(native.get("source_name"))
        token = source_id or title
        if token and token not in seen_sources:
            seen_sources.add(token)
            item = {
                key: value for key, value in (("target_id", source_id), ("title", title)) if value
            }
            add(
                "related",
                related,
                {"relation": "source", **item},
                f"/published_revision/providers/{index}",
            )

    topics = revision.get("topics", [])
    if isinstance(topics, list):
        for index, native in enumerate(topics):
            if not isinstance(native, dict):
                incomplete.add("subjects")
                continue
            identity = native.get("id")
            identity = str(identity) if type(identity) is int else _text(identity)
            label = _text(native.get("name_en")) or _text(native.get("name_cy"))
            item = {key: value for key, value in (("id", identity), ("label", label)) if value}
            if item:
                add(
                    "subjects",
                    subjects,
                    {**item, "scheme": "StatsWales topics"},
                    f"/published_revision/topics/{index}",
                )
    elif topics is not None:
        incomplete.add("subjects")

    translated = revision.get("metadata", [])
    translated = translated if isinstance(translated, list) else []
    languages = sorted(
        {
            language.lower()
            for native in translated
            if isinstance(native, dict) and (language := _text(native.get("language")))
        }
    )
    if languages:
        evidence.append(
            {
                "field": "languages",
                "source_path": "/published_revision/metadata",
                "method": "provider_assertion",
            }
        )
    links = revision.get("related_links", [])
    if isinstance(links, list):
        for index, native in enumerate(links):
            if not isinstance(native, dict) or not (url := _http_url(native.get("url"))):
                incomplete.add("related")
                continue
            title = _text(native.get("label_en")) or _text(native.get("label_cy"))
            item = {"relation": "related", "url": url, **({"title": title} if title else {})}
            add("related", related, item, f"/published_revision/related_links/{index}")
    elif links is not None:
        incomplete.add("related")
    previous = _text(revision.get("previous_revision_id"))
    if previous:
        add(
            "related",
            related,
            {"relation": "previous_revision", "target_id": previous},
            "/published_revision/previous_revision_id",
        )
    replacement = metadata.get("replaced_by")
    if isinstance(replacement, dict) and (identity := _text(replacement.get("dataset_id"))):
        title = _text(replacement.get("dataset_title"))
        add(
            "related",
            related,
            {
                "relation": "is_replaced_by",
                "target_id": identity,
                **({"title": title} if title else {}),
            },
            "/replaced_by",
        )

    fields = {
        "publishers": publishers,
        "subjects": subjects,
        "languages": languages,
        "related": related,
    }
    status = {
        field: {
            "status": "unmapped" if field in incomplete else "mapped" if values else "not_provided"
        }
        for field, values in fields.items()
    }
    for field in incomplete:
        status[field]["reason"] = (
            "Some native assertions could not be normalized; originals are retained."
        )
    status.update(
        {
            "measures": {
                "status": "unmapped",
                "reason": "Filter categories are retained; no recognized native filter identity and label establish a measure role.",
            },
            "coverage.spatial": {
                "status": "unmapped",
                "reason": "Filter categories are retained; no recognized native filter identity and label establish a geographic role.",
            },
            "licence": {
                "status": "not_provided",
                "reason": "The captured dataset and filter responses contain no licence assertion.",
            },
            "distributions": {
                "status": "not_provided",
                "reason": "The captured metadata contains no distribution links; data queries are created separately.",
            },
        }
    )
    return fields, status, evidence


def _description_fields(dataset_id, metadata):
    """Interpret explicit revision assertions without guessing dimension roles."""
    revision = metadata.get("published_revision", {})
    translated = revision.get("metadata", [])
    translated = translated if isinstance(translated, list) else []
    description, evidence = None, []
    for languages in ({"en", "en-gb"}, {"cy", "cy-gb"}):
        candidates = [
            (index, plain_text(entry.get("summary")))
            for index, entry in enumerate(translated)
            if isinstance(entry, dict)
            and str(entry.get("language", "")).lower() in languages
            and plain_text(entry.get("summary"))
        ]
        if candidates:
            # Conflicting assertions in the preferred language remain unknown.
            if len({value for _, value in candidates}) == 1:
                index, description = candidates[0]
                evidence.append(
                    {
                        "field": "description",
                        "source_path": f"/published_revision/metadata/{index}/summary",
                        "method": "provider_assertion",
                    }
                )
            break

    temporal, temporal_status, temporal_evidence = _temporal_coverage(metadata)
    evidence.extend(temporal_evidence)
    fields, status, extra_evidence = _discovery_fields(metadata)
    evidence.extend(extra_evidence)
    status["description"] = {"status": "mapped" if description else "not_provided"}
    if (
        any(isinstance(entry, dict) and plain_text(entry.get("summary")) for entry in translated)
        and not description
    ):
        status["description"] = {
            "status": "ambiguous",
            "reason": "Preferred-language summary assertions disagree.",
        }
    status["coverage.temporal"] = temporal_status
    for field, native_keys in (
        ("licence", ("licence", "license", "licence_url", "license_url")),
        ("distributions", ("distributions", "resources")),
    ):
        if any(key in native for native in (metadata, revision) for key in native_keys):
            status[field] = {
                "status": "unmapped",
                "reason": "Native assertions have an unrecognized shape; originals are retained.",
            }
    endpoint = f"{BASE}/{dataset_id}?lang=en-gb"
    evidence.append(
        {"field": "access", "source_path": "@response.url", "method": "connector_endpoint"}
    )
    return {
        **fields,
        "field_status": status,
        "description": description,
        "measures": [],
        "coverage": {"temporal": temporal} if temporal else None,
        "licence": None,
        "access": {"metadata_url": endpoint, "data_access": "not_asserted"},
        "metadata_mapping": mapping_metadata(evidence),
    }


def _temporal_coverage(metadata):
    """Keep revision dates authoritative and catalogue fallback explicitly scoped."""
    revision = metadata.get("published_revision", {})

    def bounds(source, prefix):
        result, present, invalid = {}, False, False
        for bound in ("start", "end"):
            value = source.get(f"{prefix}{bound}_date")
            if value in (None, ""):
                continue
            present = True
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                try:
                    date.fromisoformat(value)
                except ValueError:
                    pass
                else:
                    result[bound] = value
                    continue
            invalid = True
        if result.get("start", "") > result.get("end", "9999-12-31"):
            return {}, present, True
        return result, present, invalid

    selected, revision_present, invalid = bounds(revision, "coverage_")
    catalogue, catalogue_present, catalogue_invalid = bounds(metadata, "")
    basis = "/published_revision/coverage_"
    # These older catalogue dates may differ from the published revision. Never
    # fill an individual revision bound with a differently scoped assertion.
    fallback = not revision_present and len(catalogue) == 2 and not catalogue_invalid
    if fallback:
        selected, basis = catalogue, "/"
    evidence = [
        {
            "field": "coverage.temporal",
            "source_path": f"{basis}{bound}_date",
            "method": "provider_assertion",
        }
        for bound in selected
    ]
    temporal = {**selected, "scope": "provider_declared"} if selected else None
    status = {"status": "mapped" if temporal else "not_provided"}
    if fallback:
        temporal["basis"] = "dataset_catalogue_dates"
        temporal["revision_coverage"] = "not_provided"
    elif temporal and catalogue and catalogue != selected:
        temporal["basis"] = "published_revision"
        temporal["alternatives"] = [{**catalogue, "basis": "dataset_catalogue_dates"}]
        if any(catalogue[bound] != selected[bound] for bound in catalogue.keys() & selected.keys()):
            temporal["assertions_disagree"] = True
            status = {
                "status": "ambiguous",
                "reason": "Revision and catalogue dates disagree; published revision bounds take precedence.",
            }
        evidence.extend(
            {
                "field": "coverage.temporal",
                "source_path": f"/{bound}_date",
                "method": "provider_assertion",
            }
            for bound in catalogue
        )
    if invalid or (catalogue_present and catalogue_invalid):
        status = {
            "status": "unmapped",
            "reason": "Some native coverage dates are invalid or contradictory; originals are retained.",
        }
    elif not temporal and catalogue_present:
        status = {
            "status": "unmapped",
            "reason": "Catalogue fallback needs a complete valid date pair; revision coverage is absent.",
        }
    return temporal, status, evidence


# A bounded vocabulary of native filter identities AND labels. Neither a table
# title nor a matching category code establishes a statistical/geographic role.
_MEASURE_KEYS = frozenset(
    {
        "measure",
        "measurecode",
        "measureid",
        "datadescription",
        "datatypecode",
        "measure_code",
        "measureref",
        "refcode",
        "columnref",
        "datatype",
        "measures",
        "measure labels",
    }
)
_AREA_KEYS = frozenset(
    {
        "areacode",
        "areacode_pub",
        "area_code",
        "lacode",
        "lea",
        "area",
        "local_authority_code",
        "uacode",
        "leacode",
        "lhb",
        "gsscode",
        "lhbcode",
        "health_board_code",
    }
)
_AREA_LABELS = frozenset(
    {"area", "area code", "area name", "local authority", "authority", "local health board"}
)


def _dimension_roles(dimensions):
    """Project recognized native filter semantics without guessing units or GSS codes."""
    measures, areas, axes, evidence = [], [], [], []
    for dimension in dimensions:
        identity, label = dimension["id"].casefold(), str(dimension["label"]).casefold()
        measure = identity in _MEASURE_KEYS and label == "data description"
        area = identity in _AREA_KEYS and label in _AREA_LABELS
        if not (measure or area):
            continue
        role = "metric" if measure else "geo"
        dimension["role"] = role
        dimension["role_basis"] = "statswales_filter_vocabulary_v1"
        field = "measures" if measure else "coverage.spatial"
        evidence.append(
            {"field": field, "source_path": "@filters", "method": "provider_dimension_role"}
        )
        members = [
            {**member, "dimension": dimension["id"], "scheme": "StatsWales filter categories"}
            for member in dimension["values"]
        ]
        if measure:
            measures.extend(members)
        else:
            areas.extend(members)
            axes.append({"id": dimension["id"], "label": dimension["label"]})
    spatial = (
        {
            "dimensions": axes,
            "areas": areas,
            "scope": "dimension_categories",
            "basis": "statswales_filter_vocabulary_v1",
            "note": "Declared filter categories may include grouping and historical areas; observation availability is not asserted.",
        }
        if axes
        else None
    )
    return measures, spatial, evidence


def describe_dataset(dataset_id: str, client: HttpClient) -> dict:
    dataset_id = _dataset_id(dataset_id)
    metadata, native = _metadata(dataset_id, client)
    dimensions = []
    identities = set()
    for dim in native:
        identity = dim.get("factTableColumn")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise RetrievalError("StatsWales dimensions have invalid or duplicate filter keys")
        identities.add(identity)
        dimensions.append(
            {
                "id": identity,
                "label": dim.get("columnName", identity),
                "selector": identity,
                "value_count": len(list(_values(dim.get("values")))),
                "values": [
                    {"id": node["reference"], "label": node.get("description") or node["reference"]}
                    for node in _values(dim.get("values"))
                ],
                "source_metadata": dim,
            }
        )
    revision = metadata.get("published_revision", {})
    fields = _description_fields(dataset_id, metadata)
    measures, spatial, role_evidence = _dimension_roles(dimensions)
    fields["measures"] = measures
    if measures:
        fields["field_status"]["measures"] = {"status": "mapped"}
    if spatial:
        fields["coverage"] = {**(fields["coverage"] or {}), "spatial": spatial}
        fields["field_status"]["coverage.spatial"] = {"status": "mapped"}
    if dimensions:
        fields["metadata_mapping"] = mapping_metadata(
            [
                *fields["metadata_mapping"]["evidence"],
                *role_evidence,
                {"field": "dimensions", "source_path": "@filters", "method": "declared_dimension"},
            ]
        )
    fields["field_status"]["dimensions"] = {"status": "mapped" if dimensions else "not_provided"}
    result = {
        "provider": "statswales",
        "dataset_id": dataset_id,
        "title": _title(metadata, dataset_id),
        **fields,
        "dimensions": dimensions,
        "release": {
            "kind": "observed_published_revision",
            "revision_id": revision.get("id"),
            "revision_index": revision.get("revision_index"),
            "publish_at": revision.get("publish_at"),
            "updated": revision.get("updated_at"),
            "lifecycle": {
                "status": (
                    "archived"
                    if _text(metadata.get("archived_at"))
                    else "replaced"
                    if isinstance(metadata.get("replaced_by"), dict)
                    and _text(metadata["replaced_by"].get("dataset_id"))
                    else "not_asserted"
                ),
                **(
                    {"archived_at": metadata["archived_at"], "source_path": "/archived_at"}
                    if _text(metadata.get("archived_at"))
                    else {}
                ),
                **(
                    {
                        "replacement": metadata["replaced_by"],
                        "replacement_source_path": "/replaced_by",
                    }
                    if isinstance(metadata.get("replaced_by"), dict)
                    else {}
                ),
            },
            "requested_revision_pinned": False,
        },
        "capabilities": {
            "retrieval": True,
            "retrieval_reason": "The connector supports bounded StatsWales data queries.",
            "retrieval_constraints": {
                "selection_method": "server_side_native_filters",
                "published_revision_only": True,
                "bounded_query": True,
            },
            "dimension_values": True,
            "server_side_selection": True,
            "selection_syntax": "filters: [{factTableColumn: [reference, ...]}]; lang; sort_by",
            "historical_release_selection": False,
            "metadata_languages": fields["languages"],
            "retrieval_languages": ["en-gb", "cy-gb"],
        },
        "source_url": f"https://stats.gov.wales/en-GB/{dataset_id}",
        "references": [{"title": "StatsWales API documentation", "url": f"{BASE}/docs"}],
        "source_metadata": metadata,
    }
    return {**result, **normalized_metadata_fields(result)}


def list_dimension_values(
    dataset_id: str, dimension: str, client: HttpClient, *, search="", limit=20, offset=0
) -> dict:
    _window(limit, offset)
    if not isinstance(search, str) or not isinstance(dimension, str):
        raise TypeError("StatsWales dimension and search must be strings")
    _, dimensions = _metadata(dataset_id, client)
    choices = [
        dim
        for dim in dimensions
        if dimension in {dim.get("factTableColumn"), dim.get("columnName")}
    ]
    if len(choices) != 1:
        raise ValueError(f"Unknown or ambiguous StatsWales dimension: {dimension}")
    query = search.strip().casefold()
    rows, seen = [], set()
    for node in _values(choices[0].get("values")):
        code, label = node["reference"], node.get("description") or node["reference"]
        if code in seen:
            raise RetrievalError("StatsWales dimension contains duplicate reference codes")
        seen.add(code)
        if not query or query in code.casefold() or query in str(label).casefold():
            rows.append({"value": code, "label": label})
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "scope": "provider",
    }


def validate_selection(request: Request, descriptor: dict) -> None:
    """Check selectors using a retained description, without creating a query."""
    if (
        request.provider != "statswales"
        or descriptor.get("provider") != "statswales"
        or descriptor.get("dataset_id") != _dataset_id(request.dataset_id)
    ):
        raise RetrievalError("StatsWales descriptor identifies a different dataset")
    unknown = set(request.params) - {"filters", "lang", "sort_by"}
    if unknown:
        raise RetrievalError(f"Unsupported StatsWales parameters: {sorted(unknown)}")
    if request.params.get("lang", "en-gb") not in {"en", "en-gb", "cy", "cy-gb"}:
        raise RetrievalError("Unsupported StatsWales language")
    if "sort_by" in request.params and (
        not isinstance(request.params["sort_by"], str) or not request.params["sort_by"]
    ):
        raise RetrievalError("StatsWales sort_by must be a nonempty API sort string")
    native = [dim["source_metadata"] for dim in descriptor.get("dimensions", [])]
    _filters(request.params.get("filters", []), native)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _values(nodes: Any):
    if not isinstance(nodes, list):
        raise RetrievalError("StatsWales dimension values must be a list")
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("reference"), str):
            raise RetrievalError("Malformed StatsWales dimension reference")
        yield node
        if "children" in node:
            yield from _values(node["children"])


def _filters(selectors: Any, dimensions: list[dict]) -> list[dict]:
    if not isinstance(selectors, list):
        raise RetrievalError("StatsWales params.filters must be a list of selector objects")
    aliases: dict[str, list[dict]] = {}
    for dim in dimensions:
        for key in {dim.get("factTableColumn"), dim.get("columnName")}:
            if isinstance(key, str):
                aliases.setdefault(key, []).append(dim)
    result = []
    for selector in selectors:
        if not isinstance(selector, dict) or not selector:
            raise RetrievalError("StatsWales filter selectors must be nonempty objects")
        converted = {}
        for key, selected in selector.items():
            choices = aliases.get(key, [])
            if len(choices) != 1:
                raise RetrievalError(f"Unknown or ambiguous StatsWales filter column: {key}")
            if (
                not isinstance(selected, list)
                or not selected
                or any(not isinstance(code, str) for code in selected)
            ):
                raise RetrievalError(f"Filter {key} must contain a nonempty list of string codes")
            dim = choices[0]
            allowed = {item["reference"] for item in _values(dim.get("values"))}
            if any(code not in allowed for code in selected):
                raise RetrievalError(f"Unknown StatsWales reference code for {key}")
            internal = dim.get("factTableColumn")
            if not isinstance(internal, str) or not internal or internal in converted:
                raise RetrievalError(f"Ambiguous StatsWales internal filter column: {key}")
            converted[internal] = selected
        result.append(converted)
    return result


def _count(info: dict) -> int | None:
    value = info.get("totalLines")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetrievalError("StatsWales returned an invalid totalLines count")
    return value


def _codes(frame: pd.DataFrame, dimensions: list[dict]) -> dict:
    """Add codes only when a displayed label has exactly one explicit reference."""
    report = {}
    for dim in dimensions:
        column = dim.get("columnName")
        if not isinstance(column, str) or column not in frame:
            continue
        label_codes: dict[str, set[str]] = {}
        for item in _values(dim.get("values")):
            label = item.get("description")
            if isinstance(label, str):
                label_codes.setdefault(label.strip(), set()).add(item["reference"])
        mapping = {
            label: next(iter(codes)) for label, codes in label_codes.items() if len(codes) == 1
        }
        code_column = column + "__code"
        if code_column in frame:
            raise RetrievalError(
                f"StatsWales source column collides with recovered code: {code_column}"
            )
        frame[code_column] = pd.Series(
            [
                mapping.get(value.strip()) if isinstance(value, str) else None
                for value in frame[column]
            ],
            index=frame.index,
            dtype="string",
        )
        report[column] = {
            "code_column": code_column,
            "method": "unique_metadata_description_reference; strip padding for lookup only",
            "ambiguous_labels": sorted(
                label for label, codes in label_codes.items() if len(codes) > 1
            ),
            "unmapped_rows": int(frame[code_column].isna().sum()),
        }
    return report


def fetch(request: Request, client: HttpClient) -> Acquisition:
    if request.provider != "statswales":
        raise RetrievalError("StatsWales adapter received a different provider")
    dataset_id = _dataset_id(request.dataset_id)
    unknown = set(request.params) - {"filters", "lang", "sort_by"}
    if unknown:
        raise RetrievalError(f"Unsupported StatsWales parameters: {sorted(unknown)}")
    lang = request.params.get("lang", "en-gb")
    if lang not in {"en", "en-gb", "cy", "cy-gb"}:
        raise RetrievalError("Unsupported StatsWales language")
    if "sort_by" in request.params and (
        not isinstance(request.params["sort_by"], str) or not request.params["sort_by"]
    ):
        raise RetrievalError("StatsWales sort_by must be a nonempty API sort string")
    source = BASE + "/" + dataset_id
    before, dimensions = _metadata(dataset_id, client, lang=lang)
    body = {
        "filters": _filters(request.params.get("filters", []), dimensions),
        "options": {"data_value_type": "formatted"},
    }
    scope_token = _digest(
        {
            "provider": "statswales",
            "dataset_id": dataset_id,
            "metadata": before,
            "dimensions": dimensions,
        }
    )
    scope = getattr(client, "scope", lambda token: nullcontext())
    with scope(scope_token):
        created = client.post(source + "/data", json=body).json()
    filter_id = created.get("filterId") if isinstance(created, dict) else None
    if not isinstance(filter_id, str) or not re.fullmatch(r"[A-Za-z0-9]{12}", filter_id):
        raise RetrievalError("StatsWales query response has no valid filterId")
    query_info = client.get(source + "/query/" + filter_id).json()
    if not isinstance(query_info, dict):
        raise RetrievalError("Malformed StatsWales query metadata")
    if (
        query_info.get("datasetId", dataset_id) != dataset_id
        or query_info.get("id", filter_id) != filter_id
    ):
        raise RetrievalError("StatsWales query metadata identifies a different query")
    total = _count(query_info)
    if total is not None and total > request.max_rows:
        raise RetrievalError(f"StatsWales query has {total} rows; max_rows={request.max_rows}")
    revision_before = before.get("published_revision", {})
    if not isinstance(revision_before, dict):
        raise RetrievalError("Malformed StatsWales published revision")
    revision_id = revision_before.get("id")
    if revision_id and query_info.get("revisionId") != revision_id:
        raise RetrievalError("StatsWales query revision does not match current metadata")

    params = {"lang": lang, "format": "json", "page_size": request.page_size}
    if "sort_by" in request.params:
        params["sort_by"] = request.params["sort_by"]
    rows: list[dict] = []
    seen_pages: set[str] = set()
    page = 1
    with scope(scope_token):
        while True:
            params["page_number"] = page
            batch = client.get(source + "/data/" + filter_id, params=params).json()
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise RetrievalError("StatsWales data page is not a list of rows")
            if len(batch) > request.page_size:
                raise RetrievalError("StatsWales page exceeds requested page_size")
            if total is not None:
                expected = min(request.page_size, max(total - len(rows), 0))
                if len(batch) != expected:
                    raise RetrievalError(
                        "StatsWales page length disagrees with totalLines; incomplete data"
                    )
            if batch:
                fingerprint = _digest(batch)
                if fingerprint in seen_pages:
                    raise RetrievalError(
                        "StatsWales repeated a nonempty page; pagination did not progress"
                    )
                seen_pages.add(fingerprint)
            rows.extend(batch)
            if len(rows) > request.max_rows:
                raise RetrievalError(f"StatsWales result exceeds max_rows={request.max_rows}")
            if (total is not None and len(rows) == total) or len(batch) < request.page_size:
                break
            page += 1
    after = client.get(source, params={"lang": lang}).json()
    if not isinstance(after, dict) or _digest(revision_before) != _digest(
        after.get("published_revision", {})
    ):
        raise RetrievalError("StatsWales published revision changed during retrieval")
    if total is not None and len(rows) != total:
        raise RetrievalError("StatsWales result does not match totalLines")
    query_after = client.get(source + "/query/" + filter_id).json()
    if (
        not isinstance(query_after, dict)
        or _count(query_after) != total
        or query_after.get("revisionId") != query_info.get("revisionId")
    ):
        raise RetrievalError("StatsWales query count or revision changed during retrieval")

    frame = pd.DataFrame(rows, dtype=object)
    recovery = _codes(frame, dimensions)
    title = _title(before, dataset_id, lang)
    release = {
        "kind": "observed_published_revision",
        "revision_id": revision_id,
        "revision_index": revision_before.get("revision_index"),
        "publish_at": revision_before.get("publish_at"),
        "query_revision_id": query_info.get("revisionId"),
        "observed_at": datetime.now(UTC).isoformat(),
        "requested_revision_pinned": False,
        "revision_stable_during_retrieval": bool(revision_id),
    }
    acquired = Acquisition(
        provider="statswales",
        dataset_id=dataset_id,
        title=title,
        frame=frame,
        metadata={
            "source_before": before,
            "source_after": after,
            "dimensions": dimensions,
            "query": query_info,
            "query_after": query_after,
            "query_creation": created,
            "effective_query": body,
            "code_recovery": recovery,
            "row_count": len(rows),
            "provider_row_count": total,
            "completeness_method": "query_totalLines_before_after_and_page_lengths"
            if total is not None
            else "terminal_short_or_empty_page; no provider count",
            "documentation_drift": [
                "POST /data omits totalLines; GET /query supplies it",
                "human-name filters failed live; selectors use metadata factTableColumn",
                "formatted_extended failed live; formatted cells and Notes retained",
                "page beyond totalLines returns HTTP 400; stop at independently rechecked count",
            ],
            "reference": "https://github.com/jamie-ralph/statswalesr",
        },
        release=release,
        licence="Not stated in StatsWales API metadata",
        citation=f"Welsh Government, StatsWales: {title}; published revision {revision_id or 'unknown'}",
        source_url=f"https://stats.gov.wales/{'cy-GB' if lang.startswith('cy') else 'en-GB'}/{dataset_id}",
    )
    from ssdu.normalization import normalize

    acquired.dataset = normalize(acquired, request)
    return acquired
