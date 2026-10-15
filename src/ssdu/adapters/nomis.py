"""Nomis CSV acquisition using https://www.nomisweb.co.uk/api/v01/help."""

import hashlib
import io
import json
import re
from contextlib import nullcontext

import pandas as pd

from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError

BASE = "https://www.nomisweb.co.uk/api/v01/dataset"
OGL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"


def default_request() -> Request:
    return Request(
        provider="nomis",
        dataset_id="NM_162_1",
        params={"geography": "2092957697", "date": "2025-01-2025-12"},
        page_size=5,
    )


def _families(document, *, allow_empty=False):
    try:
        structure = document["structure"]
        if (
            allow_empty
            and structure.get("keyfamilies") is None
            and structure.get("header", {}).get("id") == "none"
        ):
            return []
        values = structure["keyfamilies"]["keyfamily"]
        values = values if isinstance(values, list) else [values]
        if any(not isinstance(value, dict) for value in values):
            raise TypeError("Dataset definitions must be objects")
        return values
    except (KeyError, TypeError, AttributeError) as exc:
        raise RetrievalError("Nomis response is missing dataset definitions") from exc


def _digest(value):
    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _revisions(family):
    annotations = family.get("annotations", {}).get("annotation", [])
    if isinstance(annotations, dict):
        annotations = [annotations]
    return {
        a["annotationtitle"]: a.get("annotationtext")
        for a in annotations
        if a.get("annotationtitle") in {"LastUpdated", "LastRevised", "Status", "Units"}
    }


def search(query: str, client: HttpClient) -> list[dict]:
    if not query.strip():
        raise ValueError("Nomis search needs a keyword to avoid downloading the whole catalogue")
    document = client.get(f"{BASE}/def.sdmx.json", {"search": f"*{query.strip()}*"}).json()
    return [
        {"id": f["id"], "title": f.get("name", {}).get("value", f["id"]), "provider": "nomis"}
        for f in _families(document, allow_empty=True)
    ]


def _window(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("offset must be an integer from 0 to 1000000")


def _dataset_id(dataset_id):
    if not isinstance(dataset_id, str) or not re.fullmatch(r"NM_\d+_\d+", dataset_id):
        raise ValueError("Nomis dataset_id must look like NM_162_1")
    return dataset_id


def _dimensions(family):
    dimensions = family.get("components", {}).get("dimension", [])
    if isinstance(dimensions, dict):
        dimensions = [dimensions]
    if not isinstance(dimensions, list) or any(
        not isinstance(d, dict) or not isinstance(d.get("conceptref"), str) for d in dimensions
    ):
        raise RetrievalError("Nomis definition has malformed dimensions")
    return dimensions


def _definition(dataset_id, client):
    document = client.get(f"{BASE}/{_dataset_id(dataset_id)}/def.sdmx.json").json()
    families = _families(document)
    if len(families) != 1 or families[0].get("id") != dataset_id:
        raise RetrievalError("Nomis definition identifies a different dataset")
    return document, families[0]


def list_datasets(query: str, client: HttpClient, *, limit=20, offset=0) -> dict:
    """Page the bounded SDMX catalogue; Nomis has no catalogue offset endpoint.

    The caller's HTTP cache can reuse the complete metadata resource across
    pages. Observation downloads are never needed to browse this catalogue.
    """
    _window(limit, offset)
    if not isinstance(query, str):
        raise TypeError("Nomis query must be a string")
    params = {"search": f"*{query.strip()}*"} if query.strip() else None
    document = client.get(f"{BASE}/def.sdmx.json", params=params).json()
    rows = []
    seen = set()
    for family in _families(document, allow_empty=True):
        identity = family.get("id")
        if not isinstance(identity, str) or identity in seen:
            raise RetrievalError("Nomis catalogue contains invalid or duplicate dataset IDs")
        _dataset_id(identity)
        seen.add(identity)
        rows.append(
            {
                "id": identity,
                "title": family.get("name", {}).get("value", identity),
                "provider": "nomis",
            }
        )
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "pagination": "local_slice_of_complete_provider_metadata",
    }


def describe_dataset(dataset_id: str, client: HttpClient) -> dict:
    document, family = _definition(dataset_id, client)
    dimensions = [
        {
            "id": dim["conceptref"].lower(),
            "label": dim["conceptref"],
            "codelist": dim.get("codelist"),
            "selector": dim["conceptref"].lower(),
        }
        for dim in _dimensions(family)
    ]
    return {
        "provider": "nomis",
        "dataset_id": dataset_id,
        "title": family.get("name", {}).get("value", dataset_id),
        "dimensions": dimensions,
        "release": {
            "kind": "observed_snapshot",
            "provider_revision": _revisions(family),
            "historical_revision_addressable": False,
        },
        "capabilities": {
            "dimension_values": True,
            "server_side_selection": True,
            "selection_syntax": "Nomis native codes and expressions; date/time aliases supported",
            "unspecified_dimensions": "provider defaults; setallwherenotspecified=false",
            "historical_release_selection": False,
        },
        "source_url": f"{BASE}/{dataset_id}",
        "references": [
            {"title": "Nomis API documentation", "url": "https://www.nomisweb.co.uk/api/v01/help"}
        ],
        "source_metadata": document,
    }


def list_dimension_values(
    dataset_id: str, dimension: str, client: HttpClient, *, search="", limit=20, offset=0
) -> dict:
    _window(limit, offset)
    if not isinstance(search, str) or not isinstance(dimension, str):
        raise TypeError("Nomis dimension and search must be strings")
    _, family = _definition(dataset_id, client)
    available = {dim["conceptref"].lower() for dim in _dimensions(family)}
    if dimension not in available | ({"date", "time"} if "time" in available else set()):
        raise ValueError(f"Unknown Nomis dimension: {dimension}")
    native = "time" if dimension == "date" else dimension
    if not re.fullmatch(r"[a-z][a-z0-9_]*", native):
        raise RetrievalError("Nomis dimension has an unsupported identifier")
    params = {"search": f"*{search.strip()}*"} if search.strip() else None
    document = client.get(f"{BASE}/{dataset_id}/{native}.def.sdmx.json", params=params).json()
    try:
        lists = document["structure"]["codelists"]["codelist"]
        lists = lists if isinstance(lists, list) else [lists]
        codes = []
        for listing in lists:
            values = listing.get("code", [])
            codes.extend(values if isinstance(values, list) else [values])
        rows = []
        seen = set()
        for code in codes:
            value = code["value"]
            if not isinstance(value, str) or value in seen:
                raise RetrievalError("Nomis dimension has invalid or duplicate codes")
            seen.add(value)
            label = code.get("description", {}).get("value", value)
            rows.append({"value": value, "label": label, "source_metadata": code})
    except (KeyError, TypeError, AttributeError) as exc:
        raise RetrievalError("Nomis response is missing dimension codes") from exc
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "scope": "provider",
        "selection_context": "Provider top-level codelist; hierarchical geography branches require native expressions",
    }


def validate_selection(request: Request, descriptor: dict) -> None:
    """Validate native selector names against retained metadata, without I/O.

    Nomis range, hierarchy, relative-date and aggregate expressions remain
    provider syntax; a top-level codelist is not an exhaustive code allowlist.
    """
    if (
        request.provider != "nomis"
        or descriptor.get("provider") != "nomis"
        or descriptor.get("dataset_id") != request.dataset_id
    ):
        raise ValueError("Nomis descriptor identifies a different dataset")
    allowed = {dim["id"] for dim in descriptor.get("dimensions", [])} | {"date", "time"}
    invalid = set(request.params) - allowed
    if invalid:
        raise ValueError(f"Unsupported Nomis selectors: {sorted(invalid)}")


def fetch(request: Request, client: HttpClient) -> Acquisition:
    if request.provider != "nomis":
        raise ValueError("Nomis adapter received a different provider")
    base = f"{BASE}/{request.dataset_id}"
    definition, family = _definition(request.dataset_id, client)
    dimensions = _dimensions(family)
    allowed = {d["conceptref"].lower() for d in dimensions} | {"date", "time"}
    invalid = set(request.params) - allowed
    if invalid:
        raise ValueError(f"Unsupported Nomis selectors: {sorted(invalid)}")
    query = {
        k: ",".join(map(str, v)) if isinstance(v, list) else str(v)
        for k, v in request.params.items()
    }
    # Keep provider codes/flags rather than narrowing to just labels and values.
    query.update(
        setallwherenotspecified="false", excludezeroValues="false", excludemissingvalues="false"
    )
    frames = []
    offset, expected = 0, None
    scope_token = _digest(
        {"provider": "nomis", "dataset_id": request.dataset_id, "definition": family}
    )
    scope = getattr(client, "scope", lambda token: nullcontext())
    with scope(scope_token):
        while True:
            resource = client.get(
                f"{base}.data.csv",
                {
                    **query,
                    "recordlimit": request.page_size,
                    "recordoffset": offset,
                },
            )
            try:
                frame = pd.read_csv(io.BytesIO(resource.body), dtype=str, keep_default_na=False)
            except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
                raise RetrievalError("Nomis returned invalid CSV") from exc
            required = {"OBS_VALUE", "OBS_STATUS", "RECORD_COUNT", "RECORD_OFFSET"}
            if not required.issubset(frame.columns):
                raise RetrievalError(
                    f"Nomis response lacks columns: {sorted(required - set(frame))}"
                )
            if frame.empty:
                raise RetrievalError("Nomis returned no rows before completion; check selectors")
            if len(frame) > request.page_size:
                raise RetrievalError("Nomis page exceeds requested page_size")
            try:
                totals = {int(v) for v in frame["RECORD_COUNT"]}
                offsets = [int(v) for v in frame["RECORD_OFFSET"]]
            except ValueError as exc:
                raise RetrievalError("Nomis pagination counters are invalid") from exc
            if len(totals) != 1 or offsets != list(range(offset, offset + len(frame))):
                raise RetrievalError("Nomis returned inconsistent counts or record offsets")
            total = totals.pop()
            if expected is not None and expected != total:
                raise RetrievalError("Nomis row count changed during retrieval")
            expected = total
            if total > request.max_rows:
                raise RetrievalError(f"Nomis query has {total} rows; max_rows={request.max_rows}")
            frames.append(frame)
            offset += len(frame)
            if offset >= total:
                if offset != total:
                    raise RetrievalError("Nomis returned more rows than its declared count")
                break
    _, after = _definition(request.dataset_id, client)
    if _digest(family) != _digest(after):
        raise RetrievalError("Nomis revision changed during retrieval; retry")
    result = pd.concat(frames, ignore_index=True)
    # Numeric values are a convenience; the untouched source value and status remain.
    result["OBS_VALUE_NUMERIC"] = pd.to_numeric(result["OBS_VALUE"], errors="coerce")
    metadata = {
        "definition": definition,
        "revision_check": _revisions(after),
        "effective_query": query,
        "provider_total_rows": expected,
        "defaults": "Unspecified dimensions use provider defaults (typically totals).",
        "licence_source": "https://www.nomisweb.co.uk/home/copyright.asp",
    }
    title = family.get("name", {}).get("value", request.dataset_id)
    acquired = Acquisition(
        provider="nomis",
        dataset_id=request.dataset_id,
        title=title,
        frame=result,
        metadata=metadata,
        release={
            "kind": "observed_snapshot",
            "provider_revision": _revisions(after),
            "observed_periods": sorted(result["DATE"].unique().tolist())
            if "DATE" in result
            else [],
            "historical_revision_addressable": False,
        },
        licence=OGL,
        citation=f"Office for National Statistics via Nomis: {title}.",
        source_url=base,
    )
    from ssdu.normalization import normalize

    acquired.dataset = normalize(acquired, request)
    return acquired
