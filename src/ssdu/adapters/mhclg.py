"""Discover MHCLG publications and retrieve their tabular distributions.

Uses GOV.UK Search and Content APIs, not a release-specific asset URL. CSV is
literal records; attached CSVW metadata can declare a bounded typed schema.
No geography, measure, unit or cross-release comparability is inferred.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from urllib.parse import unquote, urljoin, urlparse

from ssdu.distributions import parse_csv
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError

PROVIDER = "mhclg"
BASE = "https://www.gov.uk"
ORGANISATIONS = (
    "ministry-of-housing-communities-local-government",
    "department-for-levelling-up-housing-and-communities",
    "department-for-communities-and-local-government",
)
HOSTS = frozenset(
    {"www.gov.uk", "assets.publishing.service.gov.uk", "open-data.communities.gov.uk"}
)
UNKNOWN_LICENCE = (
    "Licence not stated in publication metadata; consult the publication and attachment"
)
DOCUMENT_TYPES = ("national_statistics", "official_statistics", "statistical_data_set")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _url(value, base=BASE):
    if not isinstance(value, str) or not value:
        raise RetrievalError("MHCLG resource has no valid URL")
    result = urljoin(base, value)
    parsed = urlparse(result)
    try:
        valid_port = parsed.port in {None, 443}
    except ValueError:
        valid_port = False
    if (
        parsed.scheme != "https"
        or parsed.hostname not in HOSTS
        or not valid_port
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise RetrievalError("MHCLG resource URL is outside the trusted HTTPS hosts")
    return result


def _base_path(dataset_id):
    if not isinstance(dataset_id, str) or len(dataset_id) > 500:
        raise RetrievalError("MHCLG dataset_id must be a GOV.UK publication slug or base path")
    if dataset_id.startswith("https://"):
        parsed = urlparse(_url(dataset_id))
        if parsed.hostname != "www.gov.uk" or parsed.query:
            raise RetrievalError("MHCLG dataset_id must identify a GOV.UK publication")
        dataset_id = parsed.path
    if dataset_id.startswith("statdata:"):
        slug = dataset_id.removeprefix("statdata:")
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            return "/government/statistical-data-sets/" + slug
        raise RetrievalError("MHCLG statdata: identifiers require a valid publication slug")
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", dataset_id):
        return "/government/statistics/" + dataset_id
    if not re.fullmatch(
        r"/government/(?:statistics|statistical-data-sets)/[a-z0-9]+(?:-[a-z0-9]+)*", dataset_id
    ):
        raise RetrievalError("MHCLG dataset_id must be a GOV.UK publication slug or base path")
    return dataset_id


def _identifier(path):
    # Keep the less-common data-set path explicit so it resolves to the same page.
    slug = path.rsplit("/", 1)[-1]
    return slug if path.startswith("/government/statistics/") else "statdata:" + slug


def _publisher(values):
    if not isinstance(values, list):
        return False
    for entry in values:
        if isinstance(entry, dict):
            entry = entry.get("slug") or entry.get("base_path") or entry.get("link", "")
        if isinstance(entry, str) and entry.rstrip("/").rsplit("/", 1)[-1] in ORGANISATIONS:
            return True
    return False


def list_datasets(query: str, client: HttpClient, *, limit=20, offset=0):
    """Browse a bounded page of statistical publications, preserving continuation."""
    if not isinstance(query, str) or len(query) > 512:
        raise RetrievalError("MHCLG query must be a string of at most 512 characters")
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or not 0 <= offset <= 100000
    ):
        raise RetrievalError("MHCLG catalogue limit/offset are outside their bounds")
    response = client.get(
        BASE + "/api/search.json",
        params={
            "q": query,
            "filter_organisations": list(ORGANISATIONS),
            "filter_content_store_document_type": list(DOCUMENT_TYPES),
            "fields": "title,link,description,public_timestamp,organisations",
            "count": limit,
            "start": offset,
        },
    ).json()
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise RetrievalError("MHCLG catalogue response has no results list")
    total = response.get("total")
    if type(total) is not int or total < 0 or len(response["results"]) > limit:
        raise RetrievalError("MHCLG catalogue returned an invalid count")
    items = []
    for entry in response["results"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str):
            raise RetrievalError("Malformed MHCLG catalogue entry")
        if "organisations" in entry and not _publisher(entry["organisations"]):
            raise RetrievalError("MHCLG catalogue returned a different publisher")
        path = _base_path(entry.get("link"))
        items.append(
            {
                "id": _identifier(path),
                "title": entry["title"],
                "provider": PROVIDER,
                "description": entry.get("description", ""),
                "source_url": BASE + path,
                "updated": entry.get("public_timestamp"),
                "access": "open",
            }
        )
    next_offset = offset + len(items)
    if not items and offset < total:
        raise RetrievalError("MHCLG catalogue ended before its declared total")
    return {
        "items": items,
        "total": total,
        "next_offset": next_offset if next_offset < total else None,
    }


def search(query, client):
    return list_datasets(query, client, limit=25)["items"]


def _format(attachment, url):
    declared = str(attachment.get("content_type") or attachment.get("mime_type") or "").lower()
    suffix = unquote(urlparse(url).path).lower()
    if declared in {"application/csvm+json", "application/csvw+json"} or suffix.endswith(
        (".csv-metadata.json", ".csvw.json")
    ):
        return "csvw"
    if declared in {"text/csv", "application/csv"} or suffix.endswith(".csv"):
        return "csv"
    # Unknown JSON must not be treated as a valid schema without explicit selection.
    return declared or suffix.rsplit(".", 1)[-1] or "unknown"


def describe_dataset(dataset_id, client):
    path = _base_path(dataset_id)
    resource = client.get(BASE + "/api/content" + path)
    document = resource.json()
    if not isinstance(document, dict) or document.get("base_path") != path:
        raise RetrievalError("MHCLG Content API returned a different publication")
    links, details = document.get("links", {}), document.get("details", {})
    if not isinstance(links, dict) or not isinstance(details, dict):
        raise RetrievalError("Malformed MHCLG publication metadata")
    publishers = links.get("primary_publishing_organisation") or links.get("organisations")
    if not _publisher(publishers):
        raise RetrievalError("Publication is not published by MHCLG or a supported predecessor")
    title = document.get("title")
    if not isinstance(title, str) or not title:
        raise RetrievalError("MHCLG publication has no title")
    attachments = details.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 1000:
        raise RetrievalError("MHCLG publication has an invalid attachment list")
    distributions, ids = [], set()
    for item in attachments:
        if not isinstance(item, dict) or not item.get("url"):
            raise RetrievalError("Malformed MHCLG publication attachment")
        url = urljoin(BASE + path, item["url"])
        try:
            url = _url(url)
            trusted = True
        except RetrievalError:
            trusted = False
        identity = str(item.get("id") or hashlib.sha256(url.encode()).hexdigest())
        if identity in ids:
            raise RetrievalError("MHCLG publication contains repeated attachment identities")
        ids.add(identity)
        file_format = _format(item, url)
        distributions.append(
            {
                "id": identity,
                "title": item.get("title") or identity,
                "url": url,
                "format": file_format,
                "supported": trusted and file_format in {"csv", "csvw"},
                "content_type": item.get("content_type") or item.get("mime_type"),
                "bytes": item.get("file_size"),
            }
        )
    release = {
        "content_id": document.get("content_id"),
        "published": document.get("first_published_at") or details.get("first_public_at"),
        "updated": document.get("public_updated_at"),
        "revision": _digest(document),
        "metadata_sha256": hashlib.sha256(resource.body).hexdigest(),
        "release_page": BASE + path,
    }
    return {
        "provider": PROVIDER,
        "dataset_id": _identifier(path),
        "title": title,
        "description": document.get("description", ""),
        "source_url": BASE + path,
        "dimensions": [],
        "release": release,
        "distributions": distributions,
        "licence": details.get("licence")
        if isinstance(details.get("licence"), str) and details["licence"].strip()
        else UNKNOWN_LICENCE,
        "capabilities": {
            "formats": ["csv", "csvw"],
            "profile": "records",
            "selectors": "local_exact_string",
            "dimension_values": False,
            "release_history": False,
            "immutable_upstream_release": False,
        },
        "publisher": "Ministry of Housing, Communities and Local Government",
    }


def list_dimension_values(dataset_id, dimension, client, *, search="", limit=20, offset=0):
    _base_path(dataset_id)
    raise RetrievalError(
        "MHCLG publication metadata does not enumerate dimension values; inspect retained records instead"
    )


def validate_request(request):
    if request.provider != PROVIDER:
        raise RetrievalError("MHCLG adapter received a different provider")
    _base_path(request.dataset_id)
    if not isinstance(request.params, dict):
        raise RetrievalError("MHCLG params must be an object")
    unknown = set(request.params) - {
        "distribution",
        "selectors",
        "expected_revision",
        "expected_sha256",
        "table",
    }
    if unknown:
        raise RetrievalError(f"Unsupported MHCLG parameters: {sorted(unknown)}")
    for key in ("distribution", "table"):
        if key in request.params and (
            not isinstance(request.params[key], str) or not request.params[key]
        ):
            raise RetrievalError(f"MHCLG {key} must be a nonempty string")
    for key in ("expected_revision", "expected_sha256"):
        if key in request.params and (
            not isinstance(request.params[key], str)
            or not re.fullmatch(r"[0-9a-f]{64}", request.params[key])
        ):
            raise RetrievalError(f"MHCLG {key} must be a SHA-256 digest")
    selectors = request.params.get("selectors", {})
    if not isinstance(selectors, dict) or len(selectors) > 100:
        raise RetrievalError("MHCLG selectors must be a bounded object of source columns and codes")
    for column, values in selectors.items():
        if (
            not isinstance(column, str)
            or not column
            or len(column) > 2048
            or not isinstance(values, list)
            or not values
            or len(values) > 100000
            or any(not isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
        ):
            raise RetrievalError(
                "MHCLG selectors require column names and nonempty unique string-code lists"
            )


def _csvw(resource, client, table_selector):
    document = resource.json()
    if not isinstance(document, dict):
        raise RetrievalError("CSVW metadata must be an object")
    tables = document.get("tables", [document])
    if (
        not isinstance(tables, list)
        or not tables
        or any(not isinstance(table, dict) for table in tables)
    ):
        raise RetrievalError("CSVW metadata has an invalid table list")
    if table_selector:
        tables = [
            table for table in tables if table_selector in {table.get("url"), table.get("@id")}
        ]
    if len(tables) != 1:
        raise RetrievalError("CSVW table selection is missing or ambiguous; specify params.table")
    table = tables[0]
    # Inherited parsing rules are applied explicitly; semantic URI templates are
    # retained as metadata and do not turn records into statistical observations.
    schema = table.get("tableSchema", document.get("tableSchema"))
    if isinstance(schema, str):
        schema = client.get(_url(schema, resource.url)).json()
    if schema is None:
        raise RetrievalError("CSVW table requires explicit tableSchema metadata")
    dialect = table.get("dialect", document.get("dialect", {}))
    null = table.get("null", document.get("null", [""]))
    return _url(table.get("url"), resource.url), schema, dialect, null, document


def _selected(parsed, request):
    mask = parsed.source.index.to_series().map(lambda _: True)
    reverse = {name: header for header, name in parsed.column_map.items()}
    for column, values in request.params.get("selectors", {}).items():
        header = reverse.get(column)
        if header is None:
            raise RetrievalError(f"MHCLG selector names unknown column: {column}")
        missing = set(values) - set(parsed.source[header])
        if missing:
            raise RetrievalError(
                f"MHCLG selector contains codes absent from {column}: {sorted(missing)}"
            )
        mask &= parsed.source[header].isin(values)
    frame = parsed.frame.loc[mask].reset_index(drop=True)
    source = parsed.source.loc[mask].reset_index(drop=True)
    if frame.empty:
        raise RetrievalError("MHCLG selectors produce an empty table")
    if len(frame) > request.max_rows:
        raise RetrievalError(
            f"Complete MHCLG selection exceeds max_rows={request.max_rows}; no truncation"
        )
    return frame, source


def validate_selection(request, description):
    """Resolve metadata-level choices without claiming source-column validation."""
    validate_request(request)
    if description.get("provider") != PROVIDER or _base_path(
        description.get("dataset_id")
    ) != _base_path(request.dataset_id):
        raise RetrievalError("MHCLG selection describes a different publication")
    release = description["release"]
    if request.params.get("expected_revision", release["revision"]) != release["revision"]:
        raise RetrievalError("MHCLG publication revision differs from expected_revision")
    distributions = description["distributions"]
    selected = request.params.get("distribution")
    choices = (
        [item for item in distributions if item["id"] == selected]
        if selected
        else [item for item in distributions if item["supported"]]
    )
    if len(choices) != 1:
        raise RetrievalError(
            "MHCLG distribution selection is missing or ambiguous; specify params.distribution"
        )
    distribution = choices[0]
    if not distribution["supported"]:
        raise RetrievalError("MHCLG distribution has an unsupported format or untrusted URL")
    if distribution["format"] != "csvw" and "table" in request.params:
        raise RetrievalError("MHCLG params.table is only valid for CSVW distributions")
    return distribution


def fetch(request: Request, client: HttpClient) -> Acquisition:
    validate_request(request)
    description = describe_dataset(request.dataset_id, client)
    release = description["release"]
    distribution = validate_selection(request, description)
    schema, dialect, null, csvw = None, {}, None, None
    scope = getattr(client, "scope", None)
    with scope(release["revision"]) if scope else nullcontext():
        resource = client.get(_url(distribution["url"]))
        if distribution["format"] == "csvw":
            url, schema, dialect, null, csvw = _csvw(resource, client, request.params.get("table"))
            resource = client.get(url)
    # A publication can change between resolution and file acquisition. Repeated
    # metadata reads are revalidated by the shared cache when online.
    after = describe_dataset(request.dataset_id, client)
    if after["release"]["revision"] != release["revision"]:
        raise RetrievalError(
            "MHCLG publication changed during retrieval; retry the resolved release"
        )
    digest = hashlib.sha256(resource.body).hexdigest()
    if request.params.get("expected_sha256", digest) != digest:
        raise RetrievalError("MHCLG resource differs from expected_sha256")
    parsed = parse_csv(resource.body, schema=schema, dialect=dialect, null=null)
    frame, source = _selected(parsed, request)
    licence = description["licence"]
    if csvw is not None:
        declared_licence = csvw.get("dc:license") or csvw.get("dcterms:license")
        if isinstance(declared_licence, dict):
            declared_licence = declared_licence.get("@id")
        if isinstance(declared_licence, str) and declared_licence.strip():
            licence = declared_licence
    acquisition = Acquisition(
        provider=PROVIDER,
        dataset_id=request.dataset_id,
        title=description["title"],
        frame=frame,
        metadata={
            **description,
            "licence": licence,
            "columns": parsed.columns,
            "primary_key": parsed.primary_key,
            "column_map": parsed.column_map,
            "source_resource": {"url": resource.url, "sha256": digest},
            "csv_dialect": dialect,
            "csvw_schema": schema,
            "csvw_null": null,
            "csvw": csvw,
            "distribution": distribution,
            "raw_rows": len(parsed.source),
            "product_rows": len(frame),
            "complete_selection": True,
            "selectors": request.params.get("selectors", {}),
        },
        release={
            **release,
            "distribution_id": distribution["id"],
            "resource_url": resource.url,
            "resource_sha256": digest,
            "etag": resource.headers.get("etag"),
            "last_modified": resource.headers.get("last-modified"),
        },
        licence=licence,
        citation=f"MHCLG, {description['title']}. {description['source_url']}",
        source_url=resource.url,
    )
    from ssdu.normalization_mhclg import normalize

    acquisition.dataset = normalize(acquisition, request, source)
    return acquisition


def normalization_source(store, manifest):
    """Recover source lexemes from recorded, verified lineage without a live URL."""
    metadata = json.loads((store.path(manifest["product_id"]) / "metadata.json").read_bytes())
    origin = metadata.get("source_resource", {})
    pending, seen = [manifest], set()
    while pending:
        current = store.verify(pending.pop()["product_id"])
        if current["product_id"] in seen:
            continue
        seen.add(current["product_id"])
        for resource in current["resources"]:
            if resource["url"] != origin.get("url"):
                continue
            body = (store.path(current["product_id"]) / resource["file"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != origin.get("sha256"):
                raise RetrievalError(
                    "Retained MHCLG source differs from its recorded resource digest"
                )
            parsed = parse_csv(
                body,
                schema=metadata.get("csvw_schema"),
                dialect=metadata.get("csv_dialect"),
                null=metadata.get("csvw_null"),
            )
            return _selected(parsed, Request(**manifest["request"]))[1]
        pending.extend(store.verify(parent) for parent in current.get("parent_snapshot_ids", []))
    raise RetrievalError("MHCLG normalization requires its retained original CSV resource")
