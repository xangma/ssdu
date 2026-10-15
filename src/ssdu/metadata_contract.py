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

CONTRACT_VERSION = "1.0.0"
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


RECORD_FIELDS = {
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


def _record(value, names):
    _object(
        value,
        "metadata record",
        RECORD_FIELDS,
        {"provider", "native_id", "dataset_id", "title", "references", "raw_resources"},
    )
    for key in ("provider", "native_id", "dataset_id", "title"):
        _text(value[key], f"record {key}", maximum=4096 if key != "title" else 2_000_000)
    if not PROVIDER.fullmatch(value["provider"]):
        raise MetadataError("Invalid metadata provider identifier")
    if value["dataset_id"] != f"{value['provider']}:{value['native_id']}":
        raise MetadataError("Metadata dataset_id must retain provider and native identity")
    if not isinstance(value.get("kind", "dataset"), str) or value.get("kind", "dataset") not in {
        "dataset",
        "series",
    }:
        raise MetadataError("Metadata kind must be dataset or series")
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


def validate_record(record, resource_names=None):
    """Validate a detached record without supplying facts absent from the provider."""
    names = None if resource_names is None else globals()["resource_names"](resource_names)
    return _record(parse_json(canonical(record)), names)


def record_version(record):
    """Metadata revision excluding local clocks and raw-response storage locations."""
    value = validate_record(record)
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
    if value["contract_version"] != CONTRACT_VERSION:
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
    rows = [_record(row, names) for row in value["records"]]
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


def contract_schema():
    """Return the portable JSON Schema for v1's structural contract.

    Cross-record identities, report/count consistency, canonical ordering and raw
    resource existence are additionally enforced by validate_document and the store.
    Format-aware validators should enable standard date-time and URI checks.
    """
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
        "kind": {"enum": ["dataset", "series"]},
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
        "$id": f"https://xangma.github.io/ssdu/schemas/metadata-{CONTRACT_VERSION}.json",
        "title": f"SSDU provider metadata snapshot {CONTRACT_VERSION}",
        "$comment": "DCAT-aligned concepts; not a complete DCAT or DDI serialization. Store validation additionally checks evidence closure and identities.",
        "type": "object",
        "additionalProperties": False,
        "required": ["contract_version", "captured_at", "providers", "records"],
        "properties": {
            "contract_version": {"const": CONTRACT_VERSION},
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
