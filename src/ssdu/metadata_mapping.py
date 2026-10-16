"""Pure metadata interpretation helpers; no HTTP, licence defaults or inferred data extents."""

import copy
import re
from html import unescape
from html.parser import HTMLParser

MAPPING_VERSION = "1.2.0"
MAPPING_FIELDS = frozenset(
    {
        "description",
        "publishers",
        "subjects",
        "dimensions",
        "measures",
        "coverage.temporal",
        "coverage.spatial",
        "licence",
        "access",
        "distributions",
        "languages",
        "related",
    }
)
MAPPING_METHODS = frozenset(
    {"provider_assertion", "provider_dimension_role", "declared_dimension", "connector_endpoint"}
)
FIELD_STATUSES = frozenset({"mapped", "not_provided", "unmapped", "ambiguous", "not_requested"})


def normalized_metadata_fields(base):
    """Return canonical additions and honest per-field mapping status, without I/O.

    An absent normalized field does not establish that the provider omitted it.
    Adapters may assert that distinction after inspecting native metadata. Existing
    adapter statuses are retained; otherwise only retained mapping evidence can
    establish a mapped field. Provider values and the input object are untouched.
    """
    if not isinstance(base, dict):
        raise TypeError("Metadata description must be an object")
    fields = {
        key: copy.deepcopy(base.get(key) if base.get(key) is not None else [])
        for key in ("publishers", "subjects", "languages", "related")
    }
    supplied = base.get("field_status", {})
    if not isinstance(supplied, dict) or set(supplied) - MAPPING_FIELDS:
        raise ValueError("Metadata field_status must identify known normalized fields")
    status = {}
    for field, value in supplied.items():
        if (
            not isinstance(value, dict)
            or set(value) - {"status", "reason"}
            or not isinstance(value.get("status"), str)
            or value.get("status") not in FIELD_STATUSES
            or ("reason" in value and not isinstance(value["reason"], str))
        ):
            raise ValueError("Metadata field_status requires a known status and optional reason")
        status[field] = copy.deepcopy(value)
    mapping = base.get("metadata_mapping", {})
    evidence = mapping.get("evidence", []) if isinstance(mapping, dict) else []
    if not isinstance(evidence, list):
        raise TypeError("Metadata mapping evidence must be an array")
    mapped = {entry["field"] for entry in mapping_metadata(evidence)["evidence"]}
    for field in sorted(MAPPING_FIELDS - set(status)):
        value = base
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        meaningful = value not in (None, "", [], {})
        if field in mapped and meaningful:
            status[field] = {"status": "mapped"}
        else:
            status[field] = {
                "status": "unmapped",
                "reason": "No evidence-backed normalized value has been recorded.",
            }
    return {**fields, "field_status": status}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    """Retain readable provider prose without rendering HTML or following links."""
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _Text()
    parser.feed(value[:2_000_000])
    text = unescape("".join(parser.parts))
    text = re.sub(r"\[/?(?:b|i|u|p|br)\]", "", text, flags=re.IGNORECASE)
    text = "\n".join(re.sub(r"[\t\r\f\v ]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def mapping_metadata(evidence):
    """Attach deterministic native field locations to an adapter description.

    Entries contain field, source_path and method strings. The sync layer binds
    them to verified description response hashes, never to new provider requests.
    """
    import json

    unique = {}
    for item in evidence:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("field"), str)
            or item.get("field") not in MAPPING_FIELDS
            or not isinstance(item.get("method"), str)
            or item.get("method") not in MAPPING_METHODS
            or not isinstance(item.get("source_path"), str)
            or not 1 <= len(item["source_path"]) <= 8192
        ):
            raise ValueError(
                "Metadata mapping evidence requires a known field, method and source path"
            )
        unique[json.dumps(item, sort_keys=True)] = dict(item)
    return {"version": MAPPING_VERSION, "evidence": [unique[key] for key in sorted(unique)]}


def bind_mapping(descriptor, resource_paths):
    """Bind field locations to retained description responses, including sidecars.

    A description may combine multiple responses (for example metadata and filters).
    Locations refer to the corresponding native response within this evidence set;
    they are never filesystem paths or instructions to fetch another URL.
    """
    mapping = descriptor.get("metadata_mapping") if isinstance(descriptor, dict) else None
    if not isinstance(mapping, dict):
        return {"version": MAPPING_VERSION, "evidence": []}
    if mapping.get("version") != MAPPING_VERSION:
        raise ValueError("Unsupported provider metadata mapping version")
    result = mapping_metadata(mapping.get("evidence", []))
    paths = sorted(set(resource_paths))
    if result["evidence"] and not paths:
        raise ValueError("Mapped metadata fields require retained response evidence")
    result["evidence"] = [{**item, "resource_paths": paths} for item in result["evidence"]]
    return result
