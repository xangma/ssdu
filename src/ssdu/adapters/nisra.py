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
import re
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
