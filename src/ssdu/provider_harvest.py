"""Bounded metadata-only provider captures, with raw evidence for every row.

Only the shared MetadataClient performs acquisition. No observations, attachment
files or data queries are requested. Optional filter metadata supplies dimensions.
"""

import hashlib
import json
import re
from urllib.parse import unquote, urljoin, urlsplit
from uuid import UUID

from ssdu.literature_store import LiteratureError, LiteratureStore

NOMIS = "https://www.nomisweb.co.uk/api/v01/dataset/def.sdmx.json"
STATSWALES = "https://api.stats.gov.wales/v2"
GOVUK = "https://www.gov.uk/api/content/government/statistics/english-indices-of-deprivation-"


class ProviderHarvestError(LiteratureError):
    """Invalid provider metadata, plan or non-progressing pagination."""


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ProviderHarvestError(f"{label} must be an integer from {low} to {high}")
    return value


def _enabled(config, label):
    enabled = config.get("enabled", False)
    if type(enabled) is not bool:
        raise ProviderHarvestError(f"{label}.enabled must be a boolean")
    return enabled


def _plan(plan):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int:
        raise ProviderHarvestError("Provider harvest requires a schema_version=1 plan")
    if plan["schema_version"] != 1:
        raise ProviderHarvestError("Unsupported provider harvest schema_version")
    for key in ("nomis", "statswales", "deprivation"):
        if not isinstance(plan.get(key, {}), dict):
            raise ProviderHarvestError(f"{key} configuration must be an object")
    nomis = plan.get("nomis", {})
    sw = plan.get("statswales", {})
    deprivation = plan.get("deprivation", {})
    result = {
        "nomis": _enabled(nomis, "nomis"),
        "statswales": _enabled(sw, "statswales"),
        "page_size": _integer(sw.get("page_size", 1000), "page_size", 1, 1000),
        "max_pages": _integer(sw.get("max_pages", 30), "max_pages", 1, 200),
        "metadata_sample": _integer(sw.get("metadata_sample", 0), "metadata_sample", 0, 100),
        "max_requests": _integer(plan.get("max_requests", 120), "max_requests", 1, 1000),
        "max_total_bytes": _integer(
            plan.get("max_total_bytes", 128_000_000), "max_total_bytes", 1, 256_000_000
        ),
    }
    years = deprivation.get("years", [])
    if not isinstance(years, list) or len(years) > 3:
        raise ProviderHarvestError("deprivation.years must be a bounded list")
    if any(type(year) is not int or year not in {2015, 2019, 2025} for year in years):
        raise ProviderHarvestError(
            "Supported English deprivation release years are 2015, 2019, 2025"
        )
    if len(years) != len(set(years)):
        raise ProviderHarvestError("Duplicate deprivation release year")
    result["years"] = sorted(years)
    dimensions = sw.get("dimensions", False)
    if type(dimensions) is not bool:
        raise ProviderHarvestError("statswales.dimensions must be a boolean")
    keywords = sw.get("metadata_keywords", [])
    if (
        not isinstance(keywords, list)
        or len(keywords) > 20
        or any(
            not isinstance(word, str) or not word.strip() or len(word) > 200 for word in keywords
        )
    ):
        raise ProviderHarvestError(
            "statswales.metadata_keywords requires at most 20 bounded strings"
        )
    result["dimensions"] = dimensions
    result["metadata_keywords"] = [word.casefold().strip() for word in keywords]
    if not result["nomis"] and not result["statswales"] and not years:
        raise ProviderHarvestError("Enable at least one provider metadata capture")
    return result


def _json(capture, label):
    try:
        result = json.loads(capture.body)
    except (UnicodeError, ValueError) as exc:
        raise ProviderHarvestError(f"{label} is not JSON") from exc
    if not isinstance(result, dict):
        raise ProviderHarvestError(f"{label} must be a JSON object")
    return result


def _reference(source):
    return {key: source[key] for key in ("id", "title", "url")}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _description(value):
    """Bound search text; the complete provider value remains in the raw resource."""
    text = _text(value)
    return text[:2000]


def _singleton(value, label):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ProviderHarvestError(f"Invalid {label}")
    return value


class _Build:
    def __init__(self, client, offline, refresh):
        self.client, self.offline, self.refresh = client, offline, refresh
        self.resources = {}
        self.sources = []
        self.datasets = {}
        self.descriptors = {}
        self.quarantine = []
        self.coverage = {}

    def fetch(self, url, name, title, params=None):
        captured = self.client.get(
            url, params=params or {}, offline=self.offline, refresh=self.refresh
        )
        resources = captured.resources(name)
        if set(resources) & set(self.resources):
            raise ProviderHarvestError("Duplicate raw resource name")
        self.resources.update(resources)
        source = {
            "id": "provider-source:" + _digest({"url": url, "params": params or {}}),
            "title": title,
            "url": captured.metadata.get("url", url),
            "type": "provider_metadata",
            "resource_paths": sorted(resources),
            "licence": None,
        }
        self.sources.append(source)
        return captured, source

    def reject(self, provider, source, location, row, reason):
        self.quarantine.append(
            {
                "id": "quarantine:" + _digest([provider, source["id"], location]),
                "provider": provider,
                "reason": str(reason),
                "location": location,
                "resource_path": next(
                    path for path in source["resource_paths"] if not path.endswith(".capture.json")
                ),
                "raw_row": row,
                "source_ids": [source["id"]],
                "references": [_reference(source)],
            }
        )

    def add(
        self,
        provider,
        native_id,
        title,
        description,
        source,
        location,
        *,
        source_url,
        release=None,
        columns=None,
        source_metadata=None,
        coverage=None,
        scope="Provider catalogue metadata; no observations downloaded",
        licence=None,
    ):
        key = f"{provider}:{native_id}"
        if key in self.datasets:
            raise ProviderHarvestError(f"Duplicate provider dataset identity: {key}")
        ref = [_reference(source)]
        row = {
            "id": key,
            "title": title,
            "provider": provider,
            "native_id": native_id,
            "description": description,
            "doi": None,
            "version": None,
            "coverage": coverage or {},
            "source_ids": [source["id"]],
            "references": ref,
            "evidence_location": location,
            "evidence_resource": next(
                path for path in source["resource_paths"] if not path.endswith(".capture.json")
            ),
            "metadata_scope": scope,
            "retrieval_supported": False,
        }
        self.datasets[key] = row
        self.descriptors[key] = {
            "dataset_id": key,
            "native_id": native_id,
            "provider": provider,
            "title": title,
            "description": description,
            "columns": columns or [],
            "source_url": source_url,
            "licence": licence,
            "citation": f"{source['title']}: {title}. {source['url']} ({location})",
            "release": release or {},
            "source_metadata": {
                "location": location,
                "resource_path": row["evidence_resource"],
                **(source_metadata or {}),
            },
            "references": ref,
            "source_ids": [source["id"]],
            "metadata_scope": scope,
            "retrieval_supported": False,
        }
        return row, self.descriptors[key]


def _nomis(build):
    captured, source = build.fetch(NOMIS, "nomis/catalogue.json", "Nomis SDMX dataset catalogue")
    document = _json(captured, "Nomis catalogue")
    structure = document.get("structure")
    if not isinstance(structure, dict) or not isinstance(structure.get("keyfamilies"), dict):
        raise ProviderHarvestError("Nomis catalogue lacks structure.keyfamilies")
    raw_families = structure["keyfamilies"].get("keyfamily")
    families = _singleton(raw_families, "Nomis keyfamily array")
    if not families:
        raise ProviderHarvestError("Nomis whole catalogue is empty")
    dimensions_declared = 0
    for index, family in enumerate(families):
        location = "/structure/keyfamilies/keyfamily"
        if isinstance(raw_families, list):
            location += f"/{index}"
        try:
            native_id = _text(family.get("id"))
            if not re.fullmatch(r"NM_\d+_\d+", native_id):
                raise ProviderHarvestError("Missing or invalid Nomis dataset identifier")
            name = family.get("name", {})
            title = _text(name.get("value")) if isinstance(name, dict) else ""
            if not title:
                raise ProviderHarvestError("Missing Nomis dataset title")
            components = family.get("components")
            if components is not None and not isinstance(components, dict):
                raise ProviderHarvestError("Malformed Nomis components")
            dimensions = list(_singleton((components or {}).get("dimension"), "Nomis dimensions"))
            dimensions += _singleton(
                (components or {}).get("timedimension"), "Nomis time dimension"
            )
            columns = []
            for dimension in dimensions:
                concept = _text(dimension.get("conceptref"))
                if not concept:
                    raise ProviderHarvestError("Nomis dimension has no conceptref")
                columns.append(
                    {
                        "name": concept.lower(),
                        "label": concept,
                        "role": "dimension",
                        "codelist": dimension.get("codelist"),
                    }
                )
            annotations_node = family.get("annotations", {})
            if not isinstance(annotations_node, dict):
                raise ProviderHarvestError("Malformed Nomis annotations")
            annotations = _singleton(annotations_node.get("annotation"), "Nomis annotations")
            revisions = {
                entry["annotationtitle"]: entry.get("annotationtext")
                for entry in annotations
                if entry.get("annotationtitle") in {"LastUpdated", "LastRevised", "Status", "Units"}
            }
            annotations_description = "\n".join(
                _text(entry.get("annotationtext"))
                for entry in annotations
                if entry.get("annotationtitle") in {"Description", "DescriptionHtml", "Keywords"}
            )
            description_node = family.get("description", {})
            description = (
                description_node.get("value") if isinstance(description_node, dict) else None
            )
            build.add(
                "nomis",
                native_id,
                title,
                _description(description or annotations_description or title),
                source,
                location,
                source_url=f"https://www.nomisweb.co.uk/api/v01/dataset/{native_id}",
                release={
                    "kind": "catalogue_observed_revision",
                    "provider_revision": revisions,
                    "historical_revision_addressable": False,
                },
                columns=columns,
                source_metadata={
                    "dimension_declarations": dimensions,
                    "dataset_definition_version": family.get("version"),
                    "dimension_values_downloaded": False,
                    "dimension_declarations_present": "dimension" in (components or {}),
                    "description_truncated_for_index": len(
                        _text(description or annotations_description or title)
                    )
                    > 2000,
                },
            )
            dimensions_declared += int("dimension" in (components or {}))
        except ProviderHarvestError as exc:
            build.reject("nomis", source, location, family, exc)
    imported = sum(row["provider"] == "nomis" for row in build.datasets.values())
    build.coverage["nomis"] = {
        "source_rows": len(families),
        "imported": imported,
        "quarantined": len(families) - imported,
        "source_inventory_complete": True,
        "completeness_method": "single whole-catalogue SDMX keyfamily document",
        "dimension_declarations_present": dimensions_declared,
        "dimension_values_downloaded": False,
        "provider_total": None,
        "limitations": ["Provider does not supply an independently checked dataset count."],
        "references": [_reference(source)],
    }


def _pagination(document, page, page_size):
    pagination = document.get("pagination", document)
    if not isinstance(pagination, dict):
        raise ProviderHarvestError("Malformed StatsWales pagination")
    aliases = {
        "total": ("count", "total_count", "totalCount", "total_results", "totalResults", "total"),
        "pages": ("total_pages", "totalPages"),
        "page": ("page_number", "pageNumber", "current_page", "currentPage"),
        "size": ("page_size", "pageSize", "per_page", "perPage"),
    }
    counters = {}
    for name, keys in aliases.items():
        found = [pagination[key] for key in keys if key in pagination]
        if not found:
            continue
        if any(type(value) is not int or value < 0 for value in found) or len(set(found)) != 1:
            raise ProviderHarvestError(f"Invalid StatsWales pagination {name}")
        counters[name] = found[0]
    if counters.get("page", page) != page or counters.get("size", page_size) != page_size:
        raise ProviderHarvestError("StatsWales pagination does not match request")
    if "pages" in counters and "total" in counters:
        expected_pages = (counters["total"] + page_size - 1) // page_size
        if counters["pages"] != expected_pages:
            raise ProviderHarvestError("Inconsistent StatsWales total and page count")
    return counters


def _statswales(build, config):
    size = config["page_size"]
    total, pages, raw_rows, page_count = None, None, 0, 0
    seen_pages, seen_ids, refs = set(), set(), []
    terminal = False
    for page in range(1, config["max_pages"] + 1):
        captured, source = build.fetch(
            STATSWALES,
            f"statswales/catalogue-{page:04d}.json",
            f"StatsWales dataset catalogue page {page}",
            {"lang": "en-gb", "page_number": page, "page_size": size},
        )
        refs.append(_reference(source))
        document = _json(captured, "StatsWales catalogue")
        rows = document.get("data")
        if not isinstance(rows, list) or len(rows) > size:
            raise ProviderHarvestError("StatsWales catalogue requires a bounded data list")
        counters = _pagination(document, page, size)
        for field, observed in (("total", total), ("pages", pages)):
            if field in counters and observed is not None and counters[field] != observed:
                raise ProviderHarvestError("StatsWales catalogue count changed across pages")
        total = counters.get("total", total)
        pages = counters.get("pages", pages)
        if total is not None and len(rows) != min(size, max(total - raw_rows, 0)):
            raise ProviderHarvestError("StatsWales page length disagrees with declared total")
        if rows:
            fingerprint = _digest(rows)
            if fingerprint in seen_pages:
                raise ProviderHarvestError("StatsWales pagination repeated a nonempty page")
            seen_pages.add(fingerprint)
        for index, entry in enumerate(rows):
            location = f"/data/{index}"
            if isinstance(entry, dict) and entry.get("id") in seen_ids:
                raise ProviderHarvestError("StatsWales pagination repeated a dataset identifier")
            try:
                if not isinstance(entry, dict):
                    raise ProviderHarvestError("StatsWales catalogue row is not an object")
                native_id = str(UUID(entry.get("id", "")))
                if native_id in seen_ids:
                    raise ProviderHarvestError("Duplicate StatsWales dataset across pages")
                seen_ids.add(native_id)
                title = _text(entry.get("title"))
                if not title:
                    raise ProviderHarvestError("Missing StatsWales dataset title")
                build.add(
                    "statswales",
                    native_id,
                    title,
                    _description(entry.get("description") or entry.get("summary") or title),
                    source,
                    location,
                    source_url=f"https://stats.gov.wales/en-GB/{native_id}",
                    source_metadata={
                        "dimension_declarations_present": False,
                        "dimension_values_downloaded": False,
                    },
                    release={
                        "kind": "catalogue_observed_metadata",
                        "first_published_at": entry.get("first_published_at"),
                        "last_updated_at": entry.get("last_updated_at"),
                        "archived_at": entry.get("archived_at"),
                    },
                )
            except (ProviderHarvestError, ValueError, AttributeError, TypeError) as exc:
                build.reject("statswales", source, location, entry, exc)
        raw_rows += len(rows)
        page_count = page
        if (total is not None and raw_rows == total) or (pages is not None and page == pages):
            terminal = True
            break
        if len(rows) < size:
            terminal = True
            break
    keys = sorted(key for key, row in build.datasets.items() if row["provider"] == "statswales")
    sampled = 0
    selected = [
        key
        for key in keys
        if not config["metadata_keywords"]
        or any(
            word in build.datasets[key]["title"].casefold() for word in config["metadata_keywords"]
        )
    ]
    for key in selected[: config["metadata_sample"]]:
        row, descriptor = build.datasets[key], build.descriptors[key]
        captured, source = build.fetch(
            f"{STATSWALES}/{row['native_id']}",
            f"statswales/metadata-{row['native_id']}.json",
            f"StatsWales published metadata for {row['title']}",
            {"lang": "en-gb"},
        )
        metadata = _json(captured, "StatsWales published dataset metadata")
        if metadata.get("id") != row["native_id"]:
            raise ProviderHarvestError("StatsWales metadata identifies a different dataset")
        revision = metadata.get("published_revision")
        if not isinstance(revision, dict):
            raise ProviderHarvestError("StatsWales metadata lacks published_revision")
        entries = revision.get("metadata", [])
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ProviderHarvestError("Malformed StatsWales revision metadata")
        english = next(
            (
                entry
                for entry in entries
                if _text(entry.get("language")).casefold() in {"en", "en-gb"}
            ),
            {},
        )
        description = _description(english.get("summary") or row["description"])
        row["description"] = descriptor["description"] = description
        row["source_ids"].append(source["id"])
        descriptor["source_ids"].append(source["id"])
        row["references"].append(_reference(source))
        # Row and descriptor references are separately owned by the builder.
        descriptor["references"] = list(row["references"])
        descriptor["release"] = {
            "kind": "catalogue_observed_published_revision",
            "revision_id": revision.get("id"),
            "revision_index": revision.get("revision_index"),
            "publish_at": revision.get("publish_at"),
            "requested_revision_pinned": False,
        }
        descriptor["source_metadata"]["published_metadata_location"] = "/published_revision"
        descriptor["source_metadata"]["published_metadata_resource"] = next(
            path for path in source["resource_paths"] if not path.endswith(".capture.json")
        )
        for name in ("coverage_start_date", "coverage_end_date"):
            if revision.get(name) is not None:
                row["coverage"][name] = revision[name]
        if config["dimensions"]:
            capture, dimensions_source = build.fetch(
                f"{STATSWALES}/{row['native_id']}/filters",
                f"statswales/filters-{row['native_id']}.json",
                f"StatsWales dimension metadata for {row['title']}",
                {"lang": "en-gb"},
            )
            try:
                dimensions = json.loads(capture.body)
            except (UnicodeError, ValueError) as exc:
                raise ProviderHarvestError("StatsWales filters metadata is not JSON") from exc
            if not isinstance(dimensions, list):
                raise ProviderHarvestError("StatsWales filters metadata requires a list")
            columns = []
            for dimension in dimensions:
                if (
                    not isinstance(dimension, dict)
                    or not _text(dimension.get("columnName"))
                    or not _text(dimension.get("factTableColumn"))
                    or not isinstance(dimension.get("values"), list)
                ):
                    raise ProviderHarvestError("Malformed StatsWales dimension metadata")
                values_count, stack = 0, list(dimension["values"])
                while stack:
                    node = stack.pop()
                    if not isinstance(node, dict) or not isinstance(node.get("reference"), str):
                        raise ProviderHarvestError("Malformed StatsWales dimension reference")
                    values_count += 1
                    if values_count > 100_000:
                        raise ProviderHarvestError("StatsWales dimension exceeds value budget")
                    children = node.get("children", [])
                    if not isinstance(children, list):
                        raise ProviderHarvestError("Malformed StatsWales dimension children")
                    stack.extend(children)
                columns.append(
                    {
                        "name": dimension["factTableColumn"],
                        "label": dimension["columnName"],
                        "role": "dimension",
                        "declared_value_count": values_count,
                    }
                )
            row["source_ids"].append(dimensions_source["id"])
            descriptor["source_ids"].append(dimensions_source["id"])
            row["references"].append(_reference(dimensions_source))
            descriptor["references"] = list(row["references"])
            descriptor["columns"] = columns
            descriptor["source_metadata"].update(
                {
                    "dimension_declarations_present": True,
                    "dimension_values_downloaded": True,
                    "dimension_values_retained_in_raw_resource": True,
                    "dimension_metadata_resource": next(
                        path
                        for path in dimensions_source["resource_paths"]
                        if not path.endswith(".capture.json")
                    ),
                }
            )
        sampled += 1
    quarantined = sum(row["provider"] == "statswales" for row in build.quarantine)
    build.coverage["statswales"] = {
        "provider_total": total,
        "declared_pages": pages,
        "pages_captured": page_count,
        "source_rows": raw_rows,
        "imported": len(keys),
        "quarantined": quarantined,
        "missing_source_rows": max(total - raw_rows, 0) if total is not None else None,
        "source_inventory_complete": terminal,
        "budget_truncated": not terminal,
        "completeness_method": "declared_total_or_page_count"
        if total is not None or pages is not None
        else "terminal_short_or_empty_page"
        if terminal
        else "page_budget_exhausted",
        "metadata_sampled": sampled,
        "metadata_candidates": len(selected),
        "dimension_declarations_present": sampled if config["dimensions"] else 0,
        "dimension_values_downloaded": config["dimensions"] and sampled > 0,
        "limitations": [
            "Sequential observed pages are not an immutable provider catalogue release.",
            "A stable declared total does not establish unique or stable offset pagination; repeated identifiers abort publication.",
            "Column and value enumerations are not included in the catalogue endpoint.",
        ],
        "references": refs,
    }


def _deprivation(build, years):
    attachment_count = 0
    refs = []
    for year in years:
        slug = f"english-indices-of-deprivation-{year}"
        captured, source = build.fetch(
            GOVUK + str(year),
            f"deprivation/release-{year}.json",
            f"GOV.UK English indices of deprivation {year} release metadata",
        )
        refs.append(_reference(source))
        document = _json(captured, "GOV.UK deprivation release")
        title = _text(document.get("title"))
        base_path = document.get("base_path")
        expected_path = f"/government/statistics/{slug}"
        if not title or base_path != expected_path:
            raise ProviderHarvestError("GOV.UK metadata identifies a different deprivation release")
        details = document.get("details")
        if not isinstance(details, dict) or not isinstance(details.get("attachments"), list):
            raise ProviderHarvestError("GOV.UK release lacks a structured attachment inventory")
        release_url = "https://www.gov.uk" + expected_path
        release = {
            "kind": "named_release_metadata",
            "year": year,
            "first_published_at": document.get("first_published_at"),
            "public_updated_at": document.get("public_updated_at"),
            "data_files_downloaded": False,
        }
        build.add(
            "deprivation",
            slug,
            title,
            _description(document.get("description") or title),
            source,
            "",
            source_url=release_url,
            release=release,
            coverage={"country": "England", "release_year": year},
            scope="Named English deprivation release page; attachments indexed, no values",
            source_metadata={
                "attachment_count": len(details["attachments"]),
                "columns_examined": False,
            },
        )
        seen_urls = set()
        for index, attachment in enumerate(details["attachments"]):
            location = f"/details/attachments/{index}"
            try:
                if not isinstance(attachment, dict):
                    raise ProviderHarvestError("Attachment metadata is not an object")
                url = _text(attachment.get("url"))
                original_url = url
                normalization_method = None
                if url.startswith("/") and not url.startswith("//"):
                    decoded_path = unquote(urlsplit(url).path)
                    if (
                        decoded_path.startswith("//")
                        or "\\" in decoded_path
                        or any(part in {".", ".."} for part in decoded_path.split("/"))
                    ):
                        raise ProviderHarvestError("Unsafe relative GOV.UK attachment URL")
                    url = urljoin("https://www.gov.uk", url)
                    normalization_method = "leading_single_slash_against_govuk_origin"
                attachment_title = _text(attachment.get("title"))
                parts = urlsplit(url)
                if (
                    not attachment_title
                    or parts.scheme != "https"
                    or not parts.hostname
                    or parts.username
                    or parts.password
                    or parts.fragment
                ):
                    raise ProviderHarvestError("Missing or invalid attachment title/HTTPS URL")
                if url in seen_urls:
                    raise ProviderHarvestError("Duplicate attachment URL")
                seen_urls.add(url)
                native_id = slug + ":attachment-" + hashlib.sha256(url.encode()).hexdigest()[:20]
                build.add(
                    "deprivation",
                    native_id,
                    attachment_title,
                    attachment_title,
                    source,
                    location,
                    source_url=url,
                    release=release,
                    coverage={"country": "England", "release_year": year},
                    scope="Release attachment descriptor; file contents and columns unexamined",
                    source_metadata={
                        key: attachment[key]
                        for key in (
                            "content_type",
                            "file_size",
                            "number_of_pages",
                            "attachment_type",
                            "accessible",
                            "isbn",
                        )
                        if key in attachment
                    }
                    | {
                        "release_page": release_url,
                        "original_attachment_url": original_url,
                        "attachment_url_normalization": normalization_method,
                        "publisher_attachment_id": attachment.get("id"),
                        "catalogue_identity_method": "release_slug_and_attachment_url_sha256",
                        "publisher_native_dataset_id_known": False,
                    },
                )
                attachment_count += 1
            except (ProviderHarvestError, ValueError) as exc:
                build.reject("deprivation", source, location, attachment, exc)
    build.coverage["deprivation"] = {
        "release_years": years,
        "releases_imported": len(years),
        "attachments_imported": attachment_count,
        "quarantined": sum(row["provider"] == "deprivation" for row in build.quarantine),
        "source_inventory_complete": True,
        "metadata_scope": "Specified English release attachment inventories only",
        "columns_examined": False,
        "data_files_downloaded": False,
        "limitations": [
            "Documents and spreadsheets are attachment descriptors, not proven tables.",
            "Devolved-nation deprivation indices are not covered by these English releases.",
        ],
        "references": refs,
    }


def harvest_provider_metadata(root, plan, *, client=None, offline=False, refresh=False):
    """Publish a verified metadata snapshot after all requested page shapes pass.

    Each successful GET is immediately checkpointed by MetadataClient. Re-running
    the same plan reuses captures, including after a later page failed. Only an
    explicit refresh contacts providers for previously cached requests.
    """
    config = _plan(plan)
    if offline and refresh:
        raise ProviderHarvestError("offline and refresh are mutually exclusive")
    if client is None:
        from ssdu.harvest import MetadataClient

        client = MetadataClient(
            root, max_requests=config["max_requests"], max_total_bytes=config["max_total_bytes"]
        )
    build = _Build(client, offline, refresh)
    if config["nomis"]:
        _nomis(build)
    if config["statswales"]:
        _statswales(build, config)
    if config["years"]:
        _deprivation(build, config["years"])
    plan_raw = json.dumps(plan, sort_keys=True, ensure_ascii=False, indent=2).encode() + b"\n"
    build.resources["provider-harvest-plan.json"] = plan_raw
    document = {
        "schema_version": 1,
        "sources": build.sources,
        "papers": [],
        "relationships": [],
        "datasets": sorted(build.datasets.values(), key=lambda row: row["id"]),
        "catalogue_descriptors": sorted(
            build.descriptors.values(), key=lambda row: row["dataset_id"]
        ),
        "quarantine": build.quarantine,
        "coverage_report": build.coverage,
        "ingestion": {
            "kind": "provider_metadata_harvest",
            "plan": plan,
            "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
            "observations_downloaded": False,
        },
    }
    snapshot_id = LiteratureStore(root).publish(document, build.resources)
    return {
        "snapshot_id": snapshot_id,
        "counts": {
            "datasets": len(build.datasets),
            "sources": len(build.sources),
            "quarantined": len(build.quarantine),
        },
        "coverage": build.coverage,
        "offline": offline,
        "attempts": client.attempts,
        "cache_hits": client.cache_hits,
    }
