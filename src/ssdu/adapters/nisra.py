"""Bounded NISRA PxStat JSON-stat cubes, with native-code local selectors.

API reference: https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful
Search: https://github.com/CSOIreland/PxStat/wiki/API-Navigation
Selectors apply locally after downloading a complete bounded cube; page_size
does not paginate PxStat. Metadata is checked before, within and after the data
response so resumed checkpoints cannot silently mix published releases.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import nullcontext
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.normalization_nisra import (
    MAX_CUBE_CELLS,
    cube_axes,
    frame_from_cube,
    normalize,
    selected_size,
)

BASE = "https://ws-data.nisra.gov.uk/public/api.restful"
SEARCH_URL = "https://ws-data.nisra.gov.uk/public/api.jsonrpc"
DEFAULT_ID = "WDTHS"
MATRIX = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
UNKNOWN_LICENCE = "Not stated in NISRA API metadata; consult source terms"
COLLECTION_URL = f"{BASE}/PxStat.Data.Cube_API.ReadCollection"
MAX_CATALOGUE_ITEMS = 100_000
MAX_METADATA_CATEGORIES = 100_000
REFERENCES = [
    {
        "title": "PxStat RESTful cube and collection API",
        "url": "https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful",
    }
]


def _licence_reference(copyright):
    """Retain an explicit policy link, never promote its owner's name to terms."""
    href = copyright.get("href") if isinstance(copyright, dict) else None
    if not isinstance(href, str) or not href or any(char.isspace() for char in href):
        return UNKNOWN_LICENCE
    try:
        parsed = urlsplit(href)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return UNKNOWN_LICENCE
    except ValueError:
        return UNKNOWN_LICENCE
    return href


def default_request():
    """A small national weekly-deaths selection; full cube download stays bounded."""
    return Request(
        "nisra",
        DEFAULT_ID,
        {
            "selectors": {
                "STATISTIC": ["DTHSREGPROV"],
                "TLIST(W1)": ["2024W01", "2024W02", "2024W03"],
            }
        },
        max_rows=100,
    )


def validate_request(request: Request):
    if (
        request.provider != "nisra"
        or not isinstance(request.dataset_id, str)
        or not MATRIX.fullmatch(request.dataset_id)
    ):
        raise RetrievalError("NISRA dataset_id must be a native uppercase matrix code")
    if not isinstance(request.params, dict) or set(request.params) - {
        "selectors",
        "language",
        "max_cube_cells",
    }:
        raise RetrievalError(
            "Unsupported NISRA parameters; use selectors, language, max_cube_cells"
        )
    if request.params.get("language", "en") != "en":
        raise RetrievalError("NISRA adapter currently supports language=en")
    limit = request.params.get("max_cube_cells", MAX_CUBE_CELLS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CUBE_CELLS:
        raise RetrievalError(f"NISRA max_cube_cells must be 1..{MAX_CUBE_CELLS}")
    selectors = request.params.get("selectors", {})
    if not isinstance(selectors, dict) or any(
        not isinstance(axis, str)
        or not axis
        or not isinstance(values, list)
        or not values
        or any(not isinstance(code, str) or not code for code in values)
        or len(set(values)) != len(values)
        for axis, values in selectors.items()
    ):
        raise RetrievalError("NISRA selectors must map native axes to unique nonempty string codes")


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def interpretation(cube, dataset_id, *, max_cells):
    cube_axes(cube, max_cells=max_cells)
    extension = cube.get("extension")
    if not isinstance(extension, dict) or extension.get("matrix") != dataset_id:
        raise RetrievalError("NISRA metadata identifies a different matrix")
    if (
        not isinstance(cube.get("label"), str)
        or not cube["label"]
        or not isinstance(cube.get("updated"), str)
        or not cube["updated"]
    ):
        raise RetrievalError("NISRA requires an explicit title and updated release marker")
    # PxStat's data response can advertise a PX href while metadata advertises
    # JSON-stat. Those delivery links do not change interpretation or release.
    return {
        key: value for key, value in cube.items() if key not in {"value", "status", "href", "link"}
    }


def _matrix(dataset_id):
    if not isinstance(dataset_id, str) or not MATRIX.fullmatch(dataset_id):
        raise RetrievalError("NISRA dataset_id must be a native uppercase matrix code")
    return dataset_id


def _window(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if type(offset) is not int or not 0 <= offset <= 100_000:
        raise ValueError("offset must be an integer from 0 to 100000")


def list_datasets(query: str, client: HttpClient, *, limit=20, offset=0) -> dict:
    """Page a complete, byte-bounded live collection without downloading values.

    PxStat ReadCollection returns all live matrices without native pagination.
    Filtering and deterministic pagination apply locally to that collection, not
    to the older keyword-search helper's first 25 results.
    """
    _window(limit, offset)
    if not isinstance(query, str) or len(query) > 512:
        raise ValueError("NISRA catalogue query must be a string of at most 512 characters")
    collection = client.get(COLLECTION_URL).json()
    if not isinstance(collection, dict) or collection.get("error") is not None:
        raise RetrievalError("Malformed NISRA collection response")
    # The documented API envelope and NISRA's direct JSON-stat response are both
    # supported. Neither representation contains observation values.
    collection = collection.get("data", collection)
    if (
        not isinstance(collection, dict)
        or collection.get("class") != "collection"
        or collection.get("version") != "2.0"
        or not isinstance(collection.get("link"), dict)
        or not isinstance(collection["link"].get("item"), list)
    ):
        raise RetrievalError("Malformed NISRA JSON-stat collection")
    items = collection["link"]["item"]
    if len(items) > MAX_CATALOGUE_ITEMS:
        raise RetrievalError("NISRA collection exceeds the bounded catalogue item limit")
    rows, seen = [], set()
    query = query.strip().casefold()
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("extension"), dict)
            or not isinstance(item.get("label"), str)
            or not item["label"]
        ):
            raise RetrievalError("Malformed NISRA collection entry")
        identity = _matrix(item["extension"].get("matrix"))
        if identity in seen:
            raise RetrievalError("NISRA collection contains duplicate matrix codes")
        seen.add(identity)
        if not query or query in identity.casefold() or query in item["label"].casefold():
            rows.append({"id": identity, "title": item["label"], "provider": "nisra"})
    rows.sort(key=lambda row: row["id"])
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "scope": "provider",
        "pagination": "local_over_complete_live_collection",
        "complete_catalogue": True,
        "catalogue_total": len(items),
    }


def _metadata(dataset_id, client):
    dataset_id = _matrix(dataset_id)
    metadata = client.get(f"{BASE}/PxStat.Data.Cube_API.ReadMetadata/{dataset_id}/en").json()
    # Description is useful even when the cube is too large to retrieve. Bound
    # metadata categories separately from observation cardinality; never expand
    # the product of dimensions here.
    sizes = metadata.get("size") if isinstance(metadata, dict) else None
    if (
        not isinstance(sizes, list)
        or not 1 <= len(sizes) <= 64
        or any(type(size) is not int or size < 1 for size in sizes)
        or sum(sizes) > MAX_METADATA_CATEGORIES
    ):
        raise RetrievalError("NISRA metadata exceeds supported axis/category bounds")
    return interpretation(metadata, dataset_id, max_cells=math.prod(sizes))


def describe_dataset(dataset_id: str, client: HttpClient) -> dict:
    """Inspect release and native dimensions before acquiring any observations."""
    cube = _metadata(dataset_id, client)
    axes, codes, cells = cube_axes(cube, max_cells=math.prod(cube["size"]))
    return {
        "provider": "nisra",
        "dataset_id": dataset_id,
        "title": cube["label"],
        "dimensions": [
            {
                "id": axis,
                "label": cube["dimension"][axis].get("label", axis),
                "selector": axis,
                "value_count": len(codes[axis]),
                "source_metadata": cube["dimension"][axis],
            }
            for axis in axes
        ],
        "release": {
            "kind": "observed_published_revision",
            "updated": cube["updated"],
            "metadata_sha256": _digest(cube),
            "requested_revision_pinned": False,
        },
        "capabilities": {
            "dimension_values": True,
            "server_side_selection": False,
            "selection_syntax": "selectors: {native_axis: [native_code, ...]}; language=en",
            "selection_method": "complete_bounded_cube_then_native_code_local_selection",
            "historical_release_selection": False,
            "metadata_languages": ["en"],
            "retrieval_languages": ["en"],
            "max_full_cube_cells": MAX_CUBE_CELLS,
            "within_retrieval_cell_limit": cells <= MAX_CUBE_CELLS,
            "page_size_paginates_provider": False,
        },
        "full_cube_cells": cells,
        "source_url": f"{BASE}/PxStat.Data.Cube_API.ReadMetadata/{dataset_id}/en",
        "references": REFERENCES,
        "source_metadata": cube,
    }


def list_dimension_values(
    dataset_id: str, dimension: str, client: HttpClient, *, search="", limit=20, offset=0
) -> dict:
    """Page native category order from metadata, including TLIST(...) axes."""
    _window(limit, offset)
    if not isinstance(search, str) or len(search) > 512 or not isinstance(dimension, str):
        raise ValueError("NISRA dimension and bounded search must be strings")
    cube = _metadata(dataset_id, client)
    _, codes, _ = cube_axes(cube, max_cells=math.prod(cube["size"]))
    if dimension not in codes:
        raise ValueError(f"Unknown NISRA dimension: {dimension}")
    labels = cube["dimension"][dimension]["category"].get("label", {})
    query = search.strip().casefold()
    rows = [
        {"value": code, "label": labels.get(code, code)}
        for code in codes[dimension]
        if not query or query in code.casefold() or query in labels.get(code, code).casefold()
    ]
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "scope": "provider",
    }


def validate_selection(request: Request, descriptor: dict) -> None:
    """Validate retained metadata and selection without downloading a cube."""
    validate_request(request)
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("provider") != "nisra"
        or descriptor.get("dataset_id") != request.dataset_id
    ):
        raise RetrievalError("NISRA descriptor identifies a different dataset")
    limit = request.params.get("max_cube_cells", MAX_CUBE_CELLS)
    cube = descriptor.get("source_metadata")
    interpretation(cube, request.dataset_id, max_cells=limit)
    _, codes, _ = cube_axes(cube, max_cells=limit)
    count = selected_size(codes, request.params.get("selectors", {}))
    if count > request.max_rows:
        raise RetrievalError(f"NISRA selection has {count} rows; max_rows={request.max_rows}")


def search(query: str, client: HttpClient):
    """A keyword-bounded server search; return at most 25 matches."""
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 200:
        raise ValueError("NISRA search requires a keyword of 2..200 characters")
    response = client.post(
        SEARCH_URL,
        json={
            "jsonrpc": "2.0",
            "method": "PxStat.System.Navigation.Navigation_API.Search",
            "params": {"Search": query.strip(), "LngIsoCode": "en"},
            "id": "ssdu-search",
        },
    ).json()
    if (
        not isinstance(response, dict)
        or response.get("error") is not None
        or not isinstance(response.get("result"), list)
    ):
        raise RetrievalError("Malformed NISRA search response")
    rows = []
    for entry in response["result"][:25]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("MtrCode"), str)
            or not MATRIX.fullmatch(entry["MtrCode"])
            or not isinstance(entry.get("MtrTitle"), str)
        ):
            raise RetrievalError("Malformed NISRA search entry")
        rows.append({"id": entry["MtrCode"], "title": entry["MtrTitle"], "provider": "nisra"})
    return rows


def fetch(request: Request, client: HttpClient):
    validate_request(request)
    limit = request.params.get("max_cube_cells", MAX_CUBE_CELLS)
    selectors = request.params.get("selectors", {})
    meta_url = f"{BASE}/PxStat.Data.Cube_API.ReadMetadata/{request.dataset_id}/en"
    before = client.get(meta_url).json()
    identity = interpretation(before, request.dataset_id, max_cells=limit)
    _, codes, cells = cube_axes(before, max_cells=limit)
    count = selected_size(codes, selectors)
    if count > request.max_rows:
        raise RetrievalError(f"NISRA selection has {count} rows; max_rows={request.max_rows}")
    data_url = f"{BASE}/PxStat.Data.Cube_API.ReadDataset/{request.dataset_id}/JSON-stat/2.0/en"
    token = _digest(
        {"provider": "nisra", "dataset_id": request.dataset_id, "interpretation": identity}
    )
    scope = client.scope(token) if hasattr(client, "scope") else nullcontext()
    with scope:
        cube = client.get(data_url).json()
    if interpretation(cube, request.dataset_id, max_cells=limit) != identity:
        raise RetrievalError("NISRA cube metadata or release changed during retrieval")
    frame = frame_from_cube(cube, selectors, max_cells=limit)
    after = client.get(meta_url).json()
    if interpretation(after, request.dataset_id, max_cells=limit) != identity:
        raise RetrievalError("NISRA metadata or release changed during retrieval")
    title = cube["label"]
    copyright = cube["extension"].get("copyright", {})
    licence = _licence_reference(copyright)
    acquired = Acquisition(
        provider="nisra",
        dataset_id=request.dataset_id,
        title=title,
        frame=frame,
        metadata={
            "cube_metadata": {
                key: value for key, value in cube.items() if key not in {"value", "status"}
            },
            "source_before": before,
            "source_after": after,
            "selectors": selectors,
            "effective_query": request.to_dict(),
            "provider_total_rows": count,
            "full_cube_cells": cells,
            "complete_selection": True,
            "selection_method": "complete_bounded_cube_then_native_code_local_selection",
            "interpretation_sha256": _digest(identity),
            "reference": "https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful",
        },
        release={
            "kind": "observed_published_revision",
            "updated": cube["updated"],
            "metadata_sha256": _digest(identity),
            "requested_revision_pinned": False,
            "revision_stable_during_retrieval": True,
            "observed_at": datetime.now(UTC).isoformat(),
        },
        licence=licence,
        citation=f"NISRA, {title} ({request.dataset_id}); updated {cube['updated']}",
        source_url=data_url,
    )
    acquired.dataset = normalize(acquired, request)
    return acquired
