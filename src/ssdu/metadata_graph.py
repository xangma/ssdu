"""Deep, evidence-backed metadata entities independent of observation retrieval.

This is an additive contract. It neither changes nor upgrades the dataset-summary
``ssdu.metadata`` contract. Unknown source fields belong in ``attributes``;
unpublished or unrequested metadata must not be represented as known empty data.
"""

import hashlib
import re
from datetime import datetime
from urllib.parse import urlsplit

from .metadata_contract import FIELD_STATUSES, MetadataError, canonical

GRAPH_CONTRACT = "ssdu.metadata.graph"
GRAPH_CONTRACT_VERSION = "1.0.0"
ENTITY_KINDS = frozenset(
    {
        "catalogue",
        "study",
        "dataset",
        "release",
        "table",
        "instrument",
        "variable",
        "question",
        "code_list",
        "code",
    }
)
SHA256_PATTERN = r"[0-9a-f]{64}"
RELATION_PATTERN = r"[a-z][a-z0-9_]{0,99}"
MAX_ROW_BYTES = 2 * 1024 * 1024
PROVENANCE_FIELDS = frozenset({"url", "body_sha256", "pointer", "captured_at", "checked_at"})
ENTITY_FIELDS = frozenset(
    {
        "id",
        "kind",
        "catalogue",
        "origin",
        "native_id",
        "version",
        "label",
        "description",
        "attributes",
        "provenance",
    }
)


def _text(value, label, *, nullable=False, empty=False, maximum=100_000):
    if value is None and nullable:
        return
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(ord(char) < 32 and char not in "\r\n\t" for char in value)
    ):
        raise MetadataError(f"Invalid graph {label}")


def _timestamp(value):
    if value is None:
        return
    _text(value, "capture timestamp", maximum=64)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MetadataError("Invalid graph capture timestamp") from exc
    if "T" not in value or result.tzinfo is None:
        raise MetadataError("Graph capture timestamps must include time and timezone")


def validate_url(value):
    """Validate an evidence URL, without making a network request."""
    _text(value, "evidence URL", maximum=16_384)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in {None, 443}
            or any(char.isspace() for char in value)
        ):
            raise ValueError("not an absolute credential-free HTTPS URL")
    except ValueError as exc:
        raise MetadataError("Graph evidence URLs must be absolute HTTPS URLs") from exc
    return value


def validate_provenance(provenance):
    """Require retained-response identity and RFC 6901 JSON pointers.

    Null clocks explicitly mean unknown. The immutable store additionally checks
    each pointer against the exact retained response, including capture clocks.
    """
    if not isinstance(provenance, list) or not 1 <= len(provenance) <= 10_000:
        raise MetadataError("Graph provenance requires a nonempty bounded evidence list")
    normalized = []
    for ref in provenance:
        if not isinstance(ref, dict) or set(ref) != PROVENANCE_FIELDS:
            raise MetadataError("Invalid graph provenance fields")
        validate_url(ref["url"])
        if not isinstance(ref["body_sha256"], str) or not re.fullmatch(
            SHA256_PATTERN, ref["body_sha256"]
        ):
            raise MetadataError("Graph provenance requires a full response SHA-256")
        pointer = ref["pointer"]
        _text(pointer, "evidence pointer", empty=True, maximum=16_384)
        if (pointer and not pointer.startswith("/")) or re.search(r"~(?![01])", pointer):
            raise MetadataError("Graph evidence pointers must be RFC 6901 JSON pointers")
        for field in ("captured_at", "checked_at"):
            _timestamp(ref[field])
        normalized.append(dict(ref))
    return [
        dict(item)
        for item in sorted({canonical(item): item for item in normalized}.values(), key=canonical)
    ]


def entity_id(catalogue, origin, kind, native_id, version=None):
    """Content identity qualified by catalogue, origin, kind and source version.

    Titles and descriptions are deliberately absent: changes in source assertions
    produce a new snapshot, without changing the source object's identity.
    """
    for field, value in (("catalogue", catalogue), ("origin", origin), ("native ID", native_id)):
        _text(value, field, maximum=4096)
    if not isinstance(kind, str) or kind not in ENTITY_KINDS:
        raise MetadataError("Unsupported graph entity kind")
    _text(version, "version", nullable=True, maximum=4096)
    return hashlib.sha256(
        canonical(
            {
                "contract": GRAPH_CONTRACT,
                "catalogue": catalogue,
                "origin": origin,
                "kind": kind,
                "native_id": native_id,
                "version": version,
            }
        )
    ).hexdigest()


def validate_entity(entity):
    """Return a normalized copy, checking identity and finite JSON attributes."""
    if (
        not isinstance(entity, dict)
        or not ENTITY_FIELDS <= set(entity)
        or set(entity) - (ENTITY_FIELDS | {"field_status"})
    ):
        raise MetadataError("Invalid graph entity fields")
    expected = entity_id(
        entity["catalogue"],
        entity["origin"],
        entity["kind"],
        entity["native_id"],
        entity["version"],
    )
    if entity["id"] != expected:
        raise MetadataError("Graph entity ID differs from its qualified source identity")
    _text(entity["label"], "label")
    _text(entity["description"], "description", nullable=True, empty=True, maximum=1_000_000)
    if not isinstance(entity["attributes"], dict):
        raise MetadataError("Graph attributes must be an object")
    if "field_status" in entity:
        statuses = entity["field_status"]
        if not isinstance(statuses, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or value not in FIELD_STATUSES
            for key, value in statuses.items()
        ):
            raise MetadataError("Invalid graph field status")
    result = {**entity, "provenance": validate_provenance(entity["provenance"])}
    raw = canonical(result)
    if len(raw) > MAX_ROW_BYTES:
        raise MetadataError("Graph entity exceeds row byte budget")
    # Round-tripping also prevents callers mutating nested attributes after validation.
    import json

    return json.loads(raw)


def make_entity(
    catalogue,
    origin,
    kind,
    native_id,
    *,
    version=None,
    label=None,
    description=None,
    attributes=None,
    provenance,
    field_status=None,
):
    """Construct one evidence-backed entity; unknown optional metadata stays null."""
    entity = {
        "id": entity_id(catalogue, origin, kind, native_id, version),
        "catalogue": catalogue,
        "origin": origin,
        "kind": kind,
        "native_id": native_id,
        "version": version,
        "label": native_id if label is None else label,
        "description": description,
        "attributes": {} if attributes is None else attributes,
        "provenance": provenance,
    }
    if field_status is not None:
        entity["field_status"] = field_status
    return validate_entity(entity)


def validate_link(link):
    """Validate an explicit source relationship, retaining unresolved targets."""
    if not isinstance(link, dict) or set(link) != {"source", "target", "relation", "provenance"}:
        raise MetadataError("Invalid graph link fields")
    for field in ("source", "target"):
        if not isinstance(link[field], str) or not re.fullmatch(SHA256_PATTERN, link[field]):
            raise MetadataError("Graph link endpoints must be full entity SHA-256 IDs")
    if not isinstance(link["relation"], str) or not re.fullmatch(
        RELATION_PATTERN, link["relation"]
    ):
        raise MetadataError("Graph relation must be explicit lowercase snake_case")
    result = {**link, "provenance": validate_provenance(link["provenance"])}
    if len(canonical(result)) > MAX_ROW_BYTES:
        raise MetadataError("Graph link exceeds row byte budget")
    return result


def make_link(source, target, relation, *, provenance):
    """Construct a typed relationship without inventing target metadata."""
    return validate_link(
        {"source": source, "target": target, "relation": relation, "provenance": provenance}
    )


def contract_schema():
    """JSON Schema for the additive graph contract's normalized entity and link."""
    text = {"type": "string", "minLength": 1}
    identifier = {"type": "string", "pattern": f"^{SHA256_PATTERN}$"}
    provenance = {
        "type": "array",
        "minItems": 1,
        "maxItems": 10_000,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(PROVENANCE_FIELDS),
            "properties": {
                "url": {"type": "string", "format": "uri", "pattern": "^https://"},
                "body_sha256": identifier,
                "pointer": {"type": "string", "pattern": r"^(?:/(?:[^~]|~[01])*)*$"},
                "captured_at": {"type": ["string", "null"], "format": "date-time"},
                "checked_at": {"type": ["string", "null"], "format": "date-time"},
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:ssdu:metadata:graph:{GRAPH_CONTRACT_VERSION}",
        "title": "SSDU deep metadata graph",
        "x-contract": GRAPH_CONTRACT,
        "x-contract-version": GRAPH_CONTRACT_VERSION,
        "$defs": {
            "entity": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(ENTITY_FIELDS),
                "properties": {
                    "id": identifier,
                    "kind": {"enum": sorted(ENTITY_KINDS)},
                    "catalogue": text,
                    "origin": text,
                    "native_id": text,
                    "version": {"type": ["string", "null"], "minLength": 1},
                    "label": text,
                    "description": {"type": ["string", "null"]},
                    "attributes": {"type": "object"},
                    "provenance": provenance,
                    "field_status": {
                        "type": "object",
                        "additionalProperties": {"enum": sorted(FIELD_STATUSES)},
                    },
                },
            },
            "link": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "target", "relation", "provenance"],
                "properties": {
                    "source": identifier,
                    "target": identifier,
                    "relation": {"type": "string", "pattern": f"^{RELATION_PATTERN}$"},
                    "provenance": provenance,
                },
            },
        },
        "oneOf": [{"$ref": "#/$defs/entity"}, {"$ref": "#/$defs/link"}],
    }
