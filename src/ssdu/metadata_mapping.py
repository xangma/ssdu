"""Pure metadata interpretation helpers; no HTTP, licence defaults or inferred data extents."""

import re
from html import unescape
from html.parser import HTMLParser

MAPPING_VERSION = "1.1.0"
MAPPING_FIELDS = frozenset(
    {"description", "measures", "coverage.temporal", "coverage.spatial", "licence", "access"}
)
MAPPING_METHODS = frozenset(
    {"provider_assertion", "provider_dimension_role", "declared_dimension", "connector_endpoint"}
)


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
            or item.get("field") not in MAPPING_FIELDS
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
