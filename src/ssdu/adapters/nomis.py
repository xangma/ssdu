"""Nomis CSV acquisition using https://www.nomisweb.co.uk/api/v01/help."""

import io
import re

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
        return values if isinstance(values, list) else [values]
    except (KeyError, TypeError) as exc:
        raise RetrievalError("Nomis response is missing dataset definitions") from exc


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


def fetch(request: Request, client: HttpClient) -> Acquisition:
    if not re.fullmatch(r"NM_\d+_\d+", request.dataset_id):
        raise ValueError("Nomis dataset_id must look like NM_162_1")
    base = f"{BASE}/{request.dataset_id}"
    definition = client.get(f"{base}/def.sdmx.json").json()
    family = _families(definition)[0]
    components = family["components"]
    dimensions = components.get("dimension", [])
    if isinstance(dimensions, dict):
        dimensions = [dimensions]
    allowed = {d["conceptref"].lower() for d in dimensions} | {"date", "time"}
    invalid = set(request.params) - allowed
    if invalid:
        raise ValueError(f"Unsupported Nomis selectors: {sorted(invalid)}")
    if "geography" not in request.params or not ({"date", "time"} & request.params.keys()):
        raise ValueError("PoC Nomis requests require geography and date/time selectors")
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
            raise RetrievalError(f"Nomis response lacks columns: {sorted(required - set(frame))}")
        if frame.empty:
            raise RetrievalError("Nomis returned no rows before completion; check selectors")
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
    after = _families(client.get(f"{base}/def.sdmx.json").json())[0]
    if _revisions(family) != _revisions(after):
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
