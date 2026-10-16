"""Versioned provider metadata, keeping assertions, evidence and capture clocks separate.

The contract distinguishes datasets, series, distributions and access services, as
in DCAT. It is not a complete DCAT or DDI serialization. Native metadata remains
available verbatim under source_metadata and in verified raw response resources.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit

CONTRACT_VERSION = "1.1.0"
SUPPORTED_CONTRACT_VERSIONS = frozenset({"1.0.0", CONTRACT_VERSION})
FIELD_STATUSES = frozenset({"mapped", "not_provided", "unmapped", "ambiguous", "not_requested"})
NORMALIZED_FIELDS = frozenset(
    {
        "description",
        "publishers",
        "subjects",
        "dimensions",
        "measures",
        "coverage.spatial",
        "coverage.temporal",
        "licence",
        "access",
        "distributions",
        "languages",
        "related",
    }
)
MAX_RECORDS = 100_000
MAX_PROVIDERS = 100
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
PROVIDER = re.compile(r"[a-z][a-z0-9_]*")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


class MetadataError(ValueError):
    """Malformed metadata contract, missing evidence or corrupt immutable snapshot."""


def canonical(value):
    """Finite, stable UTF-8 JSON; callers retain unknown fields only where specified."""
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise MetadataError("Metadata must be finite UTF-8 JSON") from exc


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise MetadataError("Unsafe metadata resource/member path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in {".", ".."} or ":" in part for part in name.split("/"))
    ):
        raise MetadataError("Unsafe metadata resource/member path")
    return name


def resource_names(names):
    paths = {safe_name(name) for name in names}
    for name in paths:
        if any(parent.as_posix() in paths for parent in PurePosixPath(name).parents):
            raise MetadataError("Metadata resource paths conflict with directories")
    return paths


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetadataError("Duplicate metadata JSON field")
        result[key] = value
    return result


def parse_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_DOCUMENT_BYTES:
        raise MetadataError("Metadata document exceeds size budget")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MetadataError(f"Invalid metadata JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MetadataError("Metadata document must be an object")
    canonical(value)
    return value


def _object(value, label, allowed=None, required=()):
    if not isinstance(value, dict):
        raise MetadataError(f"{label} must be an object")
    if allowed is not None and set(value) - set(allowed):
        raise MetadataError(f"{label} contains unsupported fields")
    if set(required) - set(value):
        raise MetadataError(f"{label} lacks required fields")


def _text(value, label, *, nullable=False, empty=False, maximum=2_000_000):
    if value is None and nullable:
        return
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
    ):
        raise MetadataError(f"Invalid {label}")


def _time(value, label, *, nullable=False):
    if value is None and nullable:
        return
    _text(value, label, maximum=64)
    if not TIMESTAMP.fullmatch(value):
        raise MetadataError(f"{label} must include time and timezone")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MetadataError(f"Invalid {label} timestamp") from exc
    if "T" not in value or result.tzinfo is None:
        raise MetadataError(f"{label} must include time and timezone")


def _boolean(value, label):
    if type(value) is not bool:
        raise MetadataError(f"{label} must be a boolean")


def _count(value, label):
    if type(value) is not int or value < 0:
        raise MetadataError(f"{label} must be a nonnegative integer")


def _paths(paths, names, label, *, nonempty=False):
    if not isinstance(paths, list) or (nonempty and not paths):
        raise MetadataError(f"{label} must be a resource array")
    normalized = resource_names(paths)
    if len(normalized) != len(paths):
        raise MetadataError(f"Duplicate {label}")
    if names is not None and normalized - names:
        raise MetadataError(f"Missing raw metadata resource: {min(normalized - names)}")
    return sorted(normalized)


def _references(refs, names, label):
    if not isinstance(refs, list) or not refs:
        raise MetadataError(f"{label} requires readable references")
    for ref in refs:
        _object(
            ref,
            "reference",
            {"title", "url", "id", "type", "published_at", "resource_paths"},
            {"title", "url"},
        )
        for key in ("title", "url"):
            _text(ref[key], f"reference {key}")
        url = urlsplit(ref["url"])
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise MetadataError("Reference URL must be an absolute public HTTP(S) URL")
        for key in ("id", "type", "published_at"):
            if key in ref:
                _text(ref[key], f"reference {key}", nullable=True)
        if "resource_paths" in ref:
            ref["resource_paths"] = _paths(ref["resource_paths"], names, "reference paths")
    return sorted(refs, key=canonical)


LEGACY_RECORD_FIELDS = {
    "provider",
    "native_id",
    "dataset_id",
    "title",
    "description",
    "kind",
    "dimensions",
    "measures",
    "distributions",
    "release",
    "coverage",
    "access",
    "licence",
    "references",
    "raw_resources",
    "source_metadata",
    "derived_evidence",
    "captured_at",
    "last_checked_at",
    "provider_modified_at",
    "mapping_version",
    "metadata_version",
    "deleted",
    "tombstone",
}
RECORD_FIELDS = LEGACY_RECORD_FIELDS | {
    "publishers",
    "subjects",
    "languages",
    "related",
    "field_status",
}


def _url(value, label):
    _text(value, label)
    try:
        url = urlsplit(value)
    except ValueError as exc:
        raise MetadataError(f"Invalid {label} URL") from exc
    if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
        raise MetadataError(f"{label} must be an absolute public HTTP(S) URL")


def _entity(item, label):
    for name in ("id", "label", "name", "title", "role", "format", "content_type"):
        if name in item:
            _text(item[name], f"{label} {name}", nullable=True)
    for name in ("url", "download_url", "access_url"):
        if name in item and item[name] is not None:
            _url(item[name], f"{label} {name}")
    for name in ("count", "value_count", "bytes"):
        if name in item and item[name] is not None:
            _count(item[name], f"{label} {name}")
    if "supported" in item:
        _boolean(item["supported"], f"{label} supported")
    if "unit" in item and item["unit"] is not None and not isinstance(item["unit"], (str, dict)):
        raise MetadataError(f"Invalid {label} unit")
    if "values" in item and not isinstance(item["values"], list):
        raise MetadataError(f"{label} values must be an array")


def _typed_metadata(value):
    """Validate portable field shapes, retaining extensions on established fields."""
    for key, fields, identities in (
        ("publishers", {"id", "name", "url", "role"}, {"id", "name", "url"}),
        ("subjects", {"id", "label", "scheme"}, {"id", "label"}),
        ("related", {"relation", "url", "target_id", "title"}, {"url", "target_id", "title"}),
    ):
        if key not in value:
            continue
        if not isinstance(value[key], list):
            raise MetadataError(f"Record {key} must be an array of objects")
        for item in value[key]:
            _object(item, key, fields, {"relation"} if key == "related" else ())
            if not identities.intersection(item):
                raise MetadataError(f"Record {key} requires an identifying field")
            for name, field in item.items():
                (_url if name == "url" else _text)(field, f"{key} {name}")
    if "languages" in value:
        if not isinstance(value["languages"], list):
            raise MetadataError("Record languages must be an array of strings")
        for language in value["languages"]:
            _text(language, "record language", maximum=100)
    for key in ("dimensions", "measures", "distributions"):
        for item in value.get(key, []):
            _entity(item, key)
    coverage = value.get("coverage")
    if coverage is not None and not isinstance(coverage, dict):
        raise MetadataError("Record coverage must be an object or null")
    if isinstance(coverage, dict):
        for field in ("spatial", "temporal"):
            part = coverage.get(field)
            if part is None:
                continue
            _object(part, f"coverage {field}")
            for name in ("scope", "basis", "note", "start", "end"):
                if name in part:
                    _text(part[name], f"coverage {field} {name}", nullable=True)
            for name in ("dimensions", "areas", "periods"):
                if name in part and (
                    not isinstance(part[name], list)
                    or any(not isinstance(item, dict) for item in part[name])
                ):
                    raise MetadataError(f"coverage {field} {name} must be an array of objects")
                for item in part.get(name, []):
                    _entity(item, f"coverage {field} {name}")
    if "field_status" in value:
        _object(value["field_status"], "record field_status", NORMALIZED_FIELDS)
        for field, item in value["field_status"].items():
            _object(item, "field status", {"status", "reason"}, {"status"})
            if not isinstance(item["status"], str) or item["status"] not in FIELD_STATUSES:
                raise MetadataError("Invalid metadata field status")
            if "reason" in item:
                _text(item["reason"], "field status reason", empty=True)


def _record(value, names, version=CONTRACT_VERSION):
    _object(
        value,
        "metadata record",
        LEGACY_RECORD_FIELDS if version == "1.0.0" else RECORD_FIELDS,
        {"provider", "native_id", "dataset_id", "title", "references", "raw_resources"},
    )
    for key in ("provider", "native_id", "dataset_id", "title"):
        _text(value[key], f"record {key}", maximum=4096 if key != "title" else 2_000_000)
    if not PROVIDER.fullmatch(value["provider"]):
        raise MetadataError("Invalid metadata provider identifier")
    if value["dataset_id"] != f"{value['provider']}:{value['native_id']}":
        raise MetadataError("Metadata dataset_id must retain provider and native identity")
    kinds = {"dataset", "series"} | ({"publication"} if version != "1.0.0" else set())
    if (
        not isinstance(value.get("kind", "dataset"), str)
        or value.get("kind", "dataset") not in kinds
    ):
        raise MetadataError("Unsupported metadata kind")
    if "description" in value:
        _text(value["description"], "record description", nullable=True, empty=True)
    for key in ("dimensions", "measures", "distributions", "derived_evidence"):
        if key in value and (
            not isinstance(value[key], list) or any(not isinstance(x, dict) for x in value[key])
        ):
            raise MetadataError(f"Record {key} must be an array of objects")
    for evidence in value.get("derived_evidence", []):
        _text(evidence.get("method"), "derived evidence method")
        _text(evidence.get("asserted_by"), "derived evidence asserted_by")
        evidence["references"] = _references(evidence.get("references"), names, "derived evidence")
    for key in ("release", "coverage", "access", "licence"):
        if key in value and value[key] is not None and not isinstance(value[key], (dict, str)):
            raise MetadataError(f"Invalid record {key}")
    if version != "1.0.0":
        _typed_metadata(value)
    if "source_metadata" in value:
        _object(value["source_metadata"], "record source_metadata")
    for key in ("captured_at", "last_checked_at"):
        if key in value:
            _time(value[key], f"record {key}", nullable=key == "last_checked_at")
    if "provider_modified_at" in value:
        # Providers may declare only a date or an opaque revision, preserved as asserted.
        _text(value["provider_modified_at"], "provider modified date", nullable=True)
    for key in ("mapping_version", "metadata_version"):
        if key in value:
            _text(value[key], key, maximum=200)
    for key in ("deleted", "tombstone"):
        if key in value:
            _boolean(value[key], key)
    value["references"] = _references(value["references"], names, "record")
    value["raw_resources"] = _paths(value["raw_resources"], names, "raw_resources", nonempty=True)
    return value


def validate_record(record, resource_names=None, *, contract_version=CONTRACT_VERSION):
    """Validate a detached record without supplying facts absent from the provider."""
    names = None if resource_names is None else globals()["resource_names"](resource_names)
    if not isinstance(contract_version, str) or contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise MetadataError("Unsupported metadata contract_version")
    return _record(parse_json(canonical(record)), names, contract_version)


def record_version(record, *, contract_version=None):
    """Metadata revision excluding local clocks and raw-response storage locations."""
    value = validate_record(record, contract_version=contract_version or CONTRACT_VERSION)
    for field in ("captured_at", "last_checked_at", "raw_resources", "metadata_version"):
        value.pop(field, None)
    for reference in value["references"]:
        reference.pop("resource_paths", None)
    return hashlib.sha256(canonical(value)).hexdigest()


COMPLETENESS_FIELDS = {
    "status",
    "catalogue_complete",
    "descriptions_complete",
    "discovered_count",
    "described_count",
    "codes_complete",
    "code_pages",
    "quarantine_count",
    "checked_dataset_count",
    "duplicate_count",
    "reason",
    "errors",
    "observed_inventory",
    "inventory_authoritative",
    "authoritative",
}


def _provider(report, names):
    _object(
        report,
        "provider report",
        {
            "provider",
            "scope",
            "completeness",
            "freshness",
            "references",
            "raw_resources",
            "source_metadata",
        },
        {"provider", "scope", "completeness", "freshness", "references"},
    )
    if not isinstance(report["provider"], str) or not PROVIDER.fullmatch(report["provider"]):
        raise MetadataError("Invalid provider report identifier")
    _object(report["scope"], "provider scope")
    complete = report["completeness"]
    _object(
        complete,
        "completeness",
        COMPLETENESS_FIELDS,
        {
            "status",
            "catalogue_complete",
            "descriptions_complete",
            "discovered_count",
            "described_count",
        },
    )
    if not isinstance(complete["status"], str) or complete["status"] not in {
        "complete",
        "partial",
        "failed",
        "unknown",
    }:
        raise MetadataError("Unsupported completeness status")
    for key in (
        "catalogue_complete",
        "descriptions_complete",
        "codes_complete",
        "observed_inventory",
        "inventory_authoritative",
        "authoritative",
    ):
        if key in complete:
            _boolean(complete[key], f"completeness {key}")
    for key in (
        "discovered_count",
        "described_count",
        "code_pages",
        "quarantine_count",
        "checked_dataset_count",
        "duplicate_count",
    ):
        if key in complete:
            _count(complete[key], f"completeness {key}")
    if complete["described_count"] > complete["discovered_count"]:
        raise MetadataError("Described count exceeds discovered count")
    if complete.get("checked_dataset_count", 0) > complete["discovered_count"]:
        raise MetadataError("Checked dataset count exceeds discovered count")
    if (
        complete["status"] == "complete"
        and not complete["catalogue_complete"]
        and not report["scope"].get("dataset_ids")
    ):
        raise MetadataError("Complete catalogue scope requires a complete catalogue")
    if complete.get("inventory_authoritative") and (
        complete["status"] != "complete"
        or not complete["catalogue_complete"]
        or bool(report["scope"].get("query"))
        or bool(report["scope"].get("dataset_ids"))
    ):
        raise MetadataError("Only a completed unfiltered inventory can be authoritative")
    if complete["status"] == "failed" and complete["catalogue_complete"]:
        raise MetadataError("Failed provider cannot declare a complete catalogue")
    if "reason" in complete:
        _text(complete["reason"], "completeness reason", nullable=True, empty=True)
    if "errors" in complete and not isinstance(complete["errors"], list):
        raise MetadataError("Completeness errors must be an array")
    freshness = report["freshness"]
    _object(
        freshness,
        "freshness",
        {"captured_at", "last_checked_at", "provider_modified_at"},
        {"captured_at", "last_checked_at"},
    )
    _time(freshness["captured_at"], "provider captured_at")
    _time(freshness["last_checked_at"], "provider last_checked_at", nullable=True)
    if "provider_modified_at" in freshness:
        _text(freshness["provider_modified_at"], "provider modified date", nullable=True)
    report["references"] = _references(report["references"], names, "provider report")
    if "raw_resources" in report:
        report["raw_resources"] = _paths(report["raw_resources"], names, "provider raw_resources")
    if "source_metadata" in report:
        _object(report["source_metadata"], "provider source_metadata")
    return report


def validate_document(document, resource_names=None):
    """Return a canonical-order copy; reject incompatible versions and missing evidence."""
    value = parse_json(canonical(document))
    _object(
        value,
        "metadata snapshot",
        {
            "contract_version",
            "captured_at",
            "providers",
            "records",
            "scope",
            "previous_snapshot_id",
            "source_metadata",
        },
        {"contract_version", "captured_at", "providers", "records"},
    )
    if (
        not isinstance(value["contract_version"], str)
        or value["contract_version"] not in SUPPORTED_CONTRACT_VERSIONS
    ):
        raise MetadataError("Unsupported metadata contract_version")
    _time(value["captured_at"], "snapshot captured_at")
    names = None if resource_names is None else globals()["resource_names"](resource_names)
    if (
        not isinstance(value["providers"], list)
        or not 1 <= len(value["providers"]) <= MAX_PROVIDERS
    ):
        raise MetadataError("Metadata snapshot requires a bounded provider report array")
    if not isinstance(value["records"], list) or len(value["records"]) > MAX_RECORDS:
        raise MetadataError("Metadata records exceed array budget")
    reports = [_provider(item, names) for item in value["providers"]]
    by_provider = {item["provider"]: item for item in reports}
    if len(by_provider) != len(reports):
        raise MetadataError("Duplicate provider report")
    rows = [_record(row, names, value["contract_version"]) for row in value["records"]]
    identities = {row["dataset_id"] for row in rows}
    if len(identities) != len(rows):
        raise MetadataError("Duplicate metadata dataset identity")
    if any(row["provider"] not in by_provider for row in rows):
        raise MetadataError("Record lacks a provider report")
    for provider, report in by_provider.items():
        if (
            sum(row["provider"] == provider for row in rows)
            != report["completeness"]["discovered_count"]
        ):
            raise MetadataError("Provider discovered_count disagrees with retained records")
    if value.get("previous_snapshot_id") is not None and (
        not isinstance(value["previous_snapshot_id"], str)
        or not SHA256.fullmatch(value["previous_snapshot_id"])
    ):
        raise MetadataError("Invalid previous metadata snapshot identity")
    for key in ("scope", "source_metadata"):
        if key in value:
            _object(value[key], f"snapshot {key}")
    value["providers"] = sorted(reports, key=lambda item: item["provider"])
    value["records"] = sorted(rows, key=lambda item: item["dataset_id"])
    if len(canonical(value)) + 1 > MAX_DOCUMENT_BYTES:
        raise MetadataError("Normalized metadata document exceeds size budget")
    return value


def contract_schema(version=CONTRACT_VERSION):
    """Return the portable JSON Schema for v1's structural contract.

    Cross-record identities, report/count consistency, canonical ordering and raw
    resource existence are additionally enforced by validate_document and the store.
    Format-aware validators should enable standard date-time and URI checks.
    """
    if not isinstance(version, str) or version not in SUPPORTED_CONTRACT_VERSIONS:
        raise MetadataError("Unsupported metadata contract_version")
    text = {"type": "string", "minLength": 1, "maxLength": 2_000_000}
    nullable_text = {"anyOf": [text, {"type": "null"}]}
    timestamp = {
        "type": "string",
        "format": "date-time",
        "maxLength": 64,
        "pattern": f"^{TIMESTAMP.pattern}$",
    }
    nullable_time = {"anyOf": [timestamp, {"type": "null"}]}
    obj = {"type": "object"}
    flexible_value = {"type": ["string", "object", "null"]}
    object_array = {"type": "array", "items": obj}
    count = {"type": "integer", "minimum": 0}
    path = {
        "type": "string",
        "minLength": 1,
        "pattern": r"^(?!/)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)[^\\:\x00-\x1f\x7f]+$",
    }
    paths = {"type": "array", "uniqueItems": True, "items": path}
    refs = {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/reference"}}
    reference = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "url"],
        "properties": {
            "title": text,
            "url": {"type": "string", "format": "uri", "pattern": r"^https?://"},
            "id": nullable_text,
            "type": nullable_text,
            "published_at": nullable_text,
            "resource_paths": paths,
        },
    }
    record_properties = {
        "provider": {"type": "string", "pattern": f"^{PROVIDER.pattern}$"},
        "native_id": {**text, "maxLength": 4096},
        "dataset_id": {**text, "maxLength": 4096},
        "title": text,
        "description": {"type": ["string", "null"]},
        "kind": {"enum": ["dataset", "series"] + (["publication"] if version != "1.0.0" else [])},
        **{key: object_array for key in ("dimensions", "measures", "distributions")},
        **{key: flexible_value for key in ("release", "coverage", "access", "licence")},
        "references": refs,
        "raw_resources": {**paths, "minItems": 1},
        "source_metadata": obj,
        "derived_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["method", "asserted_by", "references"],
                "properties": {"method": text, "asserted_by": text, "references": refs},
            },
        },
        "captured_at": timestamp,
        "last_checked_at": nullable_time,
        "provider_modified_at": nullable_text,
        "mapping_version": {**text, "maxLength": 200},
        "metadata_version": {**text, "maxLength": 200},
        "deleted": {"type": "boolean"},
        "tombstone": {"type": "boolean"},
    }
    if version != "1.0.0":
        public_url = {
            "type": "string",
            "format": "uri",
            "pattern": r"^https?://[^/?#@\s]+(?:[/?#]|$)",
        }
        entity = {
            "type": "object",
            "properties": {
                **{
                    key: nullable_text
                    for key in ("id", "label", "name", "title", "role", "format", "content_type")
                },
                **{
                    key: {"anyOf": [public_url, {"type": "null"}]}
                    for key in ("url", "download_url", "access_url")
                },
                **{
                    key: {"anyOf": [count, {"type": "null"}]}
                    for key in ("count", "value_count", "bytes")
                },
                "supported": {"type": "boolean"},
                "unit": flexible_value,
                "values": {"type": "array"},
            },
        }
        for key in ("dimensions", "measures", "distributions"):
            record_properties[key] = {"type": "array", "items": entity}
        coverage_part = {
            "type": ["object", "null"],
            "properties": {
                **{key: nullable_text for key in ("scope", "basis", "note", "start", "end")},
                **{
                    key: {"type": "array", "items": entity}
                    for key in ("dimensions", "areas", "periods")
                },
            },
        }
        record_properties["coverage"] = {
            "type": ["object", "null"],
            "properties": {"spatial": coverage_part, "temporal": coverage_part},
        }
        for key, fields, identities in (
            ("publishers", {"id", "name", "url", "role"}, {"id", "name", "url"}),
            ("subjects", {"id", "label", "scheme"}, {"id", "label"}),
            ("related", {"relation", "url", "target_id", "title"}, {"url", "target_id", "title"}),
        ):
            shape = {
                "type": "object",
                "additionalProperties": False,
                "properties": {name: public_url if name == "url" else text for name in fields},
                "anyOf": [{"required": [name]} for name in sorted(identities)],
            }
            if key == "related":
                shape["required"] = ["relation"]
            record_properties[key] = {"type": "array", "items": shape}
        record_properties["languages"] = {"type": "array", "items": {**text, "maxLength": 100}}
        record_properties["field_status"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                field: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status"],
                    "properties": {
                        "status": {"enum": sorted(FIELD_STATUSES)},
                        "reason": {"type": "string"},
                    },
                }
                for field in sorted(NORMALIZED_FIELDS)
            },
        }
    completeness = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "catalogue_complete",
            "descriptions_complete",
            "discovered_count",
            "described_count",
        ],
        "properties": {
            "status": {"enum": ["complete", "partial", "failed", "unknown"]},
            **{
                key: {"type": "boolean"}
                for key in (
                    "catalogue_complete",
                    "descriptions_complete",
                    "codes_complete",
                    "observed_inventory",
                    "inventory_authoritative",
                    "authoritative",
                )
            },
            **{
                key: count
                for key in (
                    "discovered_count",
                    "described_count",
                    "code_pages",
                    "quarantine_count",
                    "checked_dataset_count",
                    "duplicate_count",
                )
            },
            "reason": {"type": ["string", "null"]},
            "errors": {"type": "array"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://xangma.github.io/ssdu/schemas/metadata-{version}.json",
        "title": f"SSDU provider metadata snapshot {version}",
        "$comment": "DCAT-aligned concepts; not a complete DCAT or DDI serialization. Store validation additionally checks evidence closure and identities.",
        "type": "object",
        "additionalProperties": False,
        "required": ["contract_version", "captured_at", "providers", "records"],
        "properties": {
            "contract_version": {"const": version},
            "captured_at": timestamp,
            "providers": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PROVIDERS,
                "items": {"$ref": "#/$defs/provider_report"},
            },
            "records": {
                "type": "array",
                "maxItems": MAX_RECORDS,
                "items": {"$ref": "#/$defs/record"},
            },
            "scope": obj,
            "source_metadata": obj,
            "previous_snapshot_id": {
                "anyOf": [{"type": "string", "pattern": "^[0-9a-f]{64}$"}, {"type": "null"}]
            },
        },
        "$defs": {
            "reference": reference,
            "record": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "provider",
                    "native_id",
                    "dataset_id",
                    "title",
                    "references",
                    "raw_resources",
                ],
                "properties": record_properties,
            },
            "provider_report": {
                "type": "object",
                "additionalProperties": False,
                "required": ["provider", "scope", "completeness", "freshness", "references"],
                "properties": {
                    "provider": record_properties["provider"],
                    "scope": obj,
                    "completeness": completeness,
                    "freshness": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["captured_at", "last_checked_at"],
                        "properties": {
                            "captured_at": timestamp,
                            "last_checked_at": nullable_time,
                            "provider_modified_at": nullable_text,
                        },
                    },
                    "references": refs,
                    "raw_resources": paths,
                    "source_metadata": obj,
                },
            },
        },
    }
