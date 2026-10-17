"""Current Scottish public catalogue metadata, without downloading resources.

The public CKAN API uses ``description`` rather than CKAN's usual ``notes``.
Native records are retained whole. Migration dates, coverage declarations and
resource dictionaries do not establish observation extents or semantic roles.
"""

from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from datetime import date
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from ssdu.metadata_mapping import mapping_metadata, plain_text
from ssdu.models import RetrievalError

ENDPOINT = "https://api.data.gov.scot/api/3/action/"
GUIDANCE = "https://data.gov.scot/help/technical-support/how-to-access-data-using-the-api"
KNOWN_ISSUES = "https://data.gov.scot/about/known-issues"
PREFIX = "ckan:"


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise RetrievalError("Current Scotland identifiers require the ckan: prefix")
    native = value.removeprefix(PREFIX)
    try:
        return str(UUID(native))
    except (ValueError, AttributeError):
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,199}", native):
            return native
    raise RetrievalError("Invalid current Scotland CKAN identifier")


def _uuid(value):
    try:
        identity = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RetrievalError("Scotland CKAN record requires a UUID identity") from exc
    return identity


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(ord(char) < 32 for char in value)
    ):
        return None
    return value


def _result(client, action, params):
    response = client.get(ENDPOINT + action, params=params).json()
    if not isinstance(response, dict) or response.get("success") is not True:
        raise RetrievalError(f"Scotland CKAN {action} did not return a successful result")
    return response.get("result")


def _record(value):
    if not isinstance(value, dict):
        raise RetrievalError("Malformed Scotland CKAN catalogue record")
    identity = _uuid(value.get("id"))
    if value.get("private") is True or value.get("state", "active") != "active":
        raise RetrievalError("Scotland public catalogue returned a private or inactive record")
    title = plain_text(value.get("title")) or plain_text(value.get("name")) or identity
    return identity, title


def list_datasets(query, client, *, limit=20, offset=0):
    """Page all public dataset kinds using a deterministic, bounded CKAN search."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("offset must be an integer from 0 to 1000000")
    if not isinstance(query, str) or len(query) > 512:
        raise ValueError("Scotland catalogue query must be at most 512 characters")
    result = _result(
        client,
        "package_search",
        {"q": query or "*:*", "rows": limit, "start": offset, "sort": "name asc"},
    )
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise RetrievalError("Scotland CKAN search has no results array")
    total, records = result.get("count"), result["results"]
    if type(total) is not int or total < 0 or len(records) > limit:
        raise RetrievalError("Scotland CKAN catalogue returned an invalid count or page bound")
    items, seen = [], set()
    for record in records:
        identity, title = _record(record)
        if identity in seen:
            raise RetrievalError("Scotland CKAN catalogue repeats a dataset")
        seen.add(identity)
        items.append(
            {
                "id": PREFIX + identity,
                "title": title,
                "provider": "scotland",
                "description": plain_text(record.get("description")),
                "source_url": ENDPOINT + "package_show?" + urlencode({"id": identity}),
                "updated": record.get("metadata_modified"),
                "catalogue_kind": record.get("type", "dataset"),
                "native_name": record.get("name"),
            }
        )
    following = offset + len(items)
    if (not items and offset < total) or (items and following > total):
        raise RetrievalError("Scotland CKAN catalogue page conflicts with its declared total")
    return {
        "items": items,
        "total": total,
        "next_offset": following if following < total else None,
        "pagination": "provider_offset_deterministic_name",
        "catalogue_scope": "data.gov.scot_public_ckan_catalogue",
        "compatibility": "Metadata includes resource formats not implemented for acquisition",
    }


def catalogue_membership(client):
    """Independent native name inventory for end-of-capture reconciliation."""
    values = _result(client, "package_list", {})
    if (
        not isinstance(values, list)
        or any(not isinstance(value, str) or not value for value in values)
        or len(set(values)) != len(values)
    ):
        raise RetrievalError("Scotland CKAN package_list requires distinct native names")
    return values


def catalogue_inventory(query, client, *, max_records=1000):
    """Bounded initial inventory with an independent public name reconciliation.

    Larger catalogues explicitly remain incomplete and can be traversed through
    ``list_datasets``. Neither endpoint is an atomic upstream snapshot.
    """
    if type(max_records) is not int or not 1 <= max_records <= 100_000:
        raise ValueError("max_records must be an integer from 1 to 100000")
    page = list_datasets(query, client, limit=min(max_records, 1000))
    complete = page["next_offset"] is None
    errors = []
    reconciliation = {"method": "declared_count", "atomic_upstream_snapshot": False}
    if complete and not query:
        names = catalogue_membership(client)
        observed = [item["native_name"] for item in page["items"]]
        valid_names = all(isinstance(name, str) and name for name in observed)
        matching = valid_names and set(observed) == set(names) and len(observed) == len(names)
        reconciliation.update(
            {
                "method": "package_search_count_and_package_list_names",
                "expected_count": len(names),
                "observed_count": len(observed),
                "matched": bool(matching),
            }
        )
        if not matching:
            complete = False
            errors.append({"stage": "catalogue", "code": "membership_changed_or_incomplete"})
    return {
        **page,
        "inventory_complete": complete,
        "duplicate_count": 0,
        "errors": errors,
        "reconciliation": reconciliation,
    }


def _strings(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return sorted({text for item in value if (text := plain_text(item))})


def _path(*parts):
    return "/result/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _text_field(value, *keys):
    for key in keys:
        if text := plain_text(value.get(key)):
            return text, key
    return None, None


def _boundary(value):
    """Canonical spelling and precision, without fabricating day boundaries."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]{4})(?:[-/]([0-9]{2})(?:-([0-9]{2}))?|(-Q[1-4]))?", value)
    if not match or not 1 <= int(match[1]) <= 9999:
        return None
    if match[3] and "/" in value:
        return None
    year = int(match[1])
    if match[4]:
        first = 3 * (int(match[4][-1]) - 1) + 1
        return value, "quarter", (year, first, 1), (year, first + 2, monthrange(year, first + 2)[1])
    if not match[2]:
        return value, "year", (year, 1, 1), (year, 12, 31)
    month = int(match[2])
    if not 1 <= month <= 12:
        return None
    if not match[3]:
        return (
            f"{year:04d}-{month:02d}",
            "month",
            (year, month, 1),
            (year, month, monthrange(year, month)[1]),
        )
    try:
        day = date(year, month, int(match[3]))
    except ValueError:
        return None
    return day.isoformat(), "day", (year, month, day.day), (year, month, day.day)


def describe_dataset(dataset_id, client):
    """Normalize the current portal's asserted metadata; never follow resource URLs."""
    native = _identifier(dataset_id)
    record = _result(client, "package_show", {"id": native})
    identity, title = _record(record)
    if native not in {identity, record.get("name")}:
        raise RetrievalError("Scotland CKAN returned a different dataset")
    evidence = []
    statuses = {}

    def asserted(field, path):
        evidence.append({"field": field, "source_path": path, "method": "provider_assertion"})

    def status(field, mapped, *paths, reason=None, native_present=False):
        statuses[field] = {
            "status": "mapped" if mapped else "unmapped" if native_present else "not_provided"
        }
        if reason:
            statuses[field]["reason"] = reason
        if mapped:
            for path in paths:
                asserted(field, path)

    description = plain_text(record.get("description"))
    status(
        "description",
        bool(description),
        "/result/description",
        native_present=bool(record.get("description")),
    )
    publishers = []
    publisher_paths = []
    organization = record.get("organization")
    if isinstance(organization, dict):
        name, key = _text_field(organization, "title", "name")
        if name:
            publishers.append({"name": name, "id": str(organization.get("id") or name)})
            publisher_paths.append(_path("organization", key))
            if organization.get("id"):
                publisher_paths.append(_path("organization", "id"))
    for index, item in enumerate(
        record.get("publisher", []) if isinstance(record.get("publisher"), list) else []
    ):
        paths = []
        if isinstance(item, str) and (name := plain_text(item)):
            candidate = {"name": name}
            paths.append(_path("publisher", index))
        elif isinstance(item, dict):
            name, key = _text_field(item, "title", "name")
            candidate = {"name": name} if name else None
            if candidate:
                paths.append(_path("publisher", index, key))
            if candidate and isinstance(item.get("id"), str) and item["id"]:
                candidate["id"] = item["id"]
                paths.append(_path("publisher", index, "id"))
            if candidate and (url := _url(item.get("url"))):
                candidate["url"] = url
                paths.append(_path("publisher", index, "url"))
        else:
            candidate = None
        if candidate and candidate not in publishers:
            publishers.append(candidate)
            publisher_paths.extend(paths)
    status(
        "publishers",
        bool(publishers),
        *publisher_paths,
        native_present=bool(organization or record.get("publisher")),
    )
    subjects, subject_paths = [], []
    themes = record.get("theme")
    if isinstance(themes, str):
        themes = [themes]
    for index, item in enumerate(themes if isinstance(themes, list) else []):
        if isinstance(item, dict):
            subject_identity = plain_text(item.get("name"))
            label, key = _text_field(item, "title", "name")
            paths = [_path("theme", index, key)] if label else []
            if subject_identity:
                paths.append(_path("theme", index, "name"))
        else:
            subject_identity = label = plain_text(item)
            paths = [
                _path("theme", index) if isinstance(record.get("theme"), list) else _path("theme")
            ]
        if label:
            subject = {"label": label, "scheme": "data.gov.scot/theme"}
            if subject_identity:
                subject["id"] = subject_identity
            subjects.append(subject)
            subject_paths.extend(paths)
    for index, item in enumerate(
        record.get("tags", []) if isinstance(record.get("tags"), list) else []
    ):
        if isinstance(item, dict):
            label, key = _text_field(item, "display_name", "name")
            path = _path("tags", index, key) if label else None
        else:
            label, path = plain_text(item), _path("tags", index)
        if label:
            subjects.append({"label": label, "scheme": "data.gov.scot/tag"})
            subject_paths.append(path)
    subjects = [
        dict(json.loads(value))
        for value in sorted({json.dumps(x, sort_keys=True) for x in subjects})
    ]
    status(
        "subjects",
        bool(subjects),
        *subject_paths,
        native_present=bool(record.get("theme") or record.get("tags")),
    )
    languages = _strings(record.get("language"))
    status(
        "languages",
        bool(languages),
        "/result/language",
        native_present=bool(record.get("language")),
    )
    coverage = {}
    levels = _strings(record.get("geographical_coverage"))
    spatial_paths = []
    if levels:
        coverage["spatial"] = {
            "levels": [{"id": value} for value in levels],
            "level_scheme": "data.gov.scot/geographical_coverage",
            "scope": "provider_declared",
            "basis": "geographical_coverage",
            "extent_status": "not_asserted",
            "note": "Catalogue geography classifications identify levels; actual observation areas are not asserted.",
        }
        spatial_paths.append("/result/geographical_coverage")
    spatial = record.get("spatial_coverage")
    if spatial and isinstance(spatial, (list, dict, str)):
        coverage.setdefault("spatial", {"scope": "provider_declared"})["native"] = spatial
        spatial_paths.append("/result/spatial_coverage")
    temporal_paths = []
    periods = record.get("reference_period")
    if isinstance(periods, list):
        periods = [
            {
                key: value
                for key, value in item.items()
                if key in {"start", "end"} and isinstance(value, str) and value
            }
            for item in periods
            if isinstance(item, dict)
        ]
        periods = [item for item in periods if item]
    else:
        periods = []
    if periods:
        temporal_paths.append("/result/reference_period")
        coverage["temporal"] = {
            "periods": periods,
            "scope": "provider_declared",
            "basis": "reference_period",
        }
        intervals = []
        for item in periods:
            start, end = _boundary(item.get("start")), _boundary(item.get("end"))
            if not (start or end) or ("start" in item and not start) or ("end" in item and not end):
                continue
            if start and end and start[2] > end[3]:
                continue
            interval = {"calendar": "gregorian"}
            for key, boundary in (("start", start), ("end", end)):
                if boundary:
                    interval[key], interval[key + "_precision"] = boundary[:2]
            intervals.append(interval)
        coverage["temporal"]["intervals"] = intervals
        coverage["temporal"]["interval_status"] = (
            "mapped" if len(intervals) == len(periods) else "partial" if intervals else "unmapped"
        )
    temporal = record.get("temporal_coverage")
    if temporal and isinstance(temporal, (list, dict, str)):
        coverage.setdefault("temporal", {"scope": "provider_declared"})["native"] = temporal
        temporal_paths.append("/result/temporal_coverage")
    status(
        "coverage.spatial",
        "spatial" in coverage,
        *spatial_paths,
        native_present=bool(record.get("geographical_coverage") or spatial),
    )
    status(
        "coverage.temporal",
        "temporal" in coverage,
        *temporal_paths,
        native_present=bool(record.get("reference_period") or temporal),
    )
    licence, licence_path = _url(record.get("license_url")), "/result/license_url"
    if not licence:
        licence, key = _text_field(record, "license_title", "license_id")
        licence_path = _path(key) if key else None
    if licence and licence.casefold() in {"notspecified", "not-specified", "unknown"}:
        licence = None
    status(
        "licence",
        bool(licence),
        *([licence_path] if licence else []),
    )
    distributions = []
    resources = record.get("resources", [])
    if not isinstance(resources, list):
        raise RetrievalError("Scotland CKAN resources must be an array")
    seen = set()
    for item in resources:
        if not isinstance(item, dict):
            raise RetrievalError("Malformed Scotland CKAN resource")
        resource_id = item.get("id")
        if not isinstance(resource_id, str) or not resource_id or resource_id in seen:
            raise RetrievalError("Scotland CKAN resource identities must be distinct")
        seen.add(resource_id)
        distribution = {
            "id": resource_id,
            "title": plain_text(item.get("title")) or plain_text(item.get("name")) or resource_id,
            "format": plain_text(item.get("format")) or "unknown",
            "supported": False,
            "retrieval_status": "metadata_only",
        }
        if url := _url(item.get("url")):
            distribution["url"] = url
        if text := plain_text(item.get("description")):
            distribution["description"] = text
        if text := plain_text(item.get("mimetype")) or plain_text(item.get("mime_type")):
            distribution["content_type"] = text
        size = item.get("file_size", item.get("size"))
        if isinstance(size, str) and size.isdecimal():
            size = int(size)
        if type(size) is int and size >= 0:
            distribution["bytes"] = size
        if text := _url(item.get("license_url")) or plain_text(item.get("license_id")):
            distribution["licence"] = text
        distributions.append(distribution)
    status("distributions", bool(distributions), "/result/resources")
    related, related_paths = [], []
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for format_name, value in sorted(metadata.items()):
            if url := _url(value):
                related.append(
                    {"relation": "alternate_metadata", "url": url, "title": str(format_name)}
                )
                related_paths.append(_path("metadata", format_name))
    for relation in ("relationships_as_subject", "relationships_as_object"):
        if isinstance(record.get(relation), list):
            for index, item in enumerate(record[relation]):
                if not isinstance(item, dict):
                    continue
                target = item.get("object" if relation.endswith("subject") else "subject")
                if isinstance(target, str) and target:
                    related.append(
                        {"relation": str(item.get("type") or "related"), "target_id": target}
                    )
                    related_paths.append(
                        _path(
                            relation, index, "object" if relation.endswith("subject") else "subject"
                        )
                    )
                    if item.get("type"):
                        related_paths.append(_path(relation, index, "type"))
    status(
        "related",
        bool(related),
        *related_paths,
        native_present=bool(
            metadata
            or record.get("relationships_as_subject")
            or record.get("relationships_as_object")
        ),
    )
    metadata_url = ENDPOINT + "package_show?" + urlencode({"id": identity})
    access = {"metadata_url": metadata_url, "data_access": "not_asserted"}
    if type(record.get("isopen")) is bool:
        access["provider_open"] = record["isopen"]
        asserted("access", "/result/isopen")
    conditions = []
    if condition := plain_text(record.get("access_right")):
        conditions.append(condition)
        asserted("access", "/result/access_right")
    completeness = plain_text(record.get("completeness"))
    if completeness and re.search(
        r"\bcopyright\b|\blicen[cs]e (?:terms|conditions|agreement)\b"
        r"|\bterms (?:and conditions|of (?:use|reuse))\b",
        completeness,
        re.IGNORECASE,
    ):
        conditions.append(completeness)
        asserted("access", "/result/completeness")
    if conditions:
        access["conditions"] = "\n\n".join(dict.fromkeys(conditions))
    status("access", True, "/result/id")
    statuses["dimensions"] = {
        "status": "not_provided",
        "reason": "The catalogue does not assert statistical dimension roles; resource dictionaries remain in native metadata.",
    }
    statuses["measures"] = {
        "status": "not_provided",
        "reason": "The catalogue does not assert statistical measure roles; no roles are inferred from titles or column names.",
    }
    return {
        "provider": "scotland",
        "dataset_id": dataset_id,
        "title": title,
        "description": description,
        "dimensions": [],
        "measures": [],
        "publishers": publishers,
        "subjects": subjects,
        "languages": languages,
        "related": related,
        "coverage": coverage or None,
        "licence": licence,
        "access": access,
        "distributions": distributions,
        "release": {
            "kind": "observed_metadata_snapshot",
            "modified": record.get("metadata_modified"),
            "metadata_modified": record.get("metadata_modified"),
            "provider_last_updated": record.get("last_updated"),
            "metadata_created": record.get("metadata_created"),
            "metadata_sha256": _digest(record),
            "historical_revision_addressable": False,
            "revision_status": "metadata_checked_unpinned",
            "timestamp_limitations": "Portal migration can reset metadata dates; these timestamps do not establish data release dates.",
        },
        "capabilities": {
            "metadata_only": True,
            "acquisition": False,
            "retrieval": False,
            "retrieval_reason": "Current portal resources are discoverable but acquisition is not implemented by this connector.",
            "dimension_values": False,
            "server_side_selection": False,
            "historical_release_selection": False,
            "formats": [],
            "available_resource_formats": sorted({x["format"] for x in distributions}),
            "unsupported": ["current_portal_resource_acquisition"],
        },
        "source_url": metadata_url,
        "references": [
            {"title": "Scotland current portal API guidance", "url": GUIDANCE},
            {"title": "Scotland current portal known metadata issues", "url": KNOWN_ISSUES},
        ],
        "source_metadata": record,
        "field_status": statuses,
        "metadata_mapping": mapping_metadata(evidence),
    }
