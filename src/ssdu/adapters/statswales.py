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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pandas as pd

from ssdu.http import HttpClient
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
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise RetrievalError("StatsWales catalogue response has no data list")
    result = []
    for entry in response["data"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("title"):
            raise RetrievalError("Malformed StatsWales catalogue entry")
        result.append({"id": entry["id"], "title": entry["title"], "provider": "statswales"})
    return result


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
    try:
        dataset_id = str(UUID(request.dataset_id))
    except (ValueError, AttributeError) as exc:
        raise RetrievalError("StatsWales dataset_id must be a UUID") from exc
    unknown = set(request.params) - {"filters", "lang", "sort_by"}
    if unknown:
        raise RetrievalError(f"Unsupported StatsWales parameters: {sorted(unknown)}")
    lang = request.params.get("lang", "en-gb")
    if lang not in {"en", "en-gb", "cy", "cy-gb"}:
        raise RetrievalError("Unsupported StatsWales language")
    source = BASE + "/" + dataset_id
    before = client.get(source, params={"lang": lang}).json()
    dimensions = client.get(source + "/filters", params={"lang": lang}).json()
    if (
        not isinstance(before, dict)
        or not isinstance(dimensions, list)
        or any(not isinstance(dim, dict) for dim in dimensions)
    ):
        raise RetrievalError("Malformed StatsWales metadata or dimensions")
    body = {
        "filters": _filters(request.params.get("filters", []), dimensions),
        "options": {"data_value_type": "formatted"},
    }
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
        sort = request.params["sort_by"]
        if not isinstance(sort, str) or not sort:
            raise RetrievalError("StatsWales sort_by must be a nonempty API sort string")
        params["sort_by"] = sort
    rows: list[dict] = []
    seen_pages: set[str] = set()
    page = 1
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
    translated = revision_before.get("metadata", [])
    title = dataset_id
    for entry in translated:
        if isinstance(entry, dict) and str(entry.get("language", "")).lower() == (
            "en-gb" if lang == "en" else "cy-gb" if lang == "cy" else lang
        ):
            title = entry.get("title") or title
            break
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
