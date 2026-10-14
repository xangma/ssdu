"""Development-selected, source-bound provider metadata with portable replay.

Requests are generated from exact native identities and a closed endpoint list.
Acquisition checkpoints use MetadataClient's shared host limiter; deriving a
snapshot uses retained bytes only. Country/place completeness is never inferred
from a partial hierarchy or from publication/question text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import date
from uuid import UUID

from .discovery_benchmark import BenchmarkBundle, load_bundle, rank
from .harvest import MetadataClient
from .literature_ingest import Capture
from .literature_store import LiteratureError, LiteratureStore
from .models import RetrievalError

NOMIS = "https://www.nomisweb.co.uk/api/v01/dataset"
STATSWALES = "https://api.stats.gov.wales/v2"
NOMIS_HELP = "https://www.nomisweb.co.uk/api/v01/help"
SHA = re.compile(r"[0-9a-f]{64}")
MAX_VALUES = 100_000


class EnrichmentError(LiteratureError):
    """Invalid plan, metadata identity, or retained derivation."""


def _bytes(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise EnrichmentError(f"{name} must be an integer from {lower} to {upper}")
    return value


def _native(provider, native):
    if not isinstance(native, str):
        return False
    if provider == "nomis":
        return re.fullmatch(r"NM_\d+_\d+", native) is not None
    if provider == "statswales":
        try:
            return str(UUID(native)) == native
        except (ValueError, AttributeError):
            return False
    return False


def _requests(provider, native):
    if not _native(provider, native):
        raise EnrichmentError("Metadata acquisition requires an exact native provider identity")
    if provider == "nomis":
        return [
            {"kind": kind, "url": f"{NOMIS}/{native}/{tail}", "params": {}}
            for kind, tail in (
                ("definition", "def.sdmx.json"),
                ("time", "time.def.sdmx.json"),
                ("geography", "geography.def.sdmx.json"),
            )
        ]
    return [
        {"kind": kind, "url": f"{STATSWALES}/{native}{tail}", "params": {"lang": "en-gb"}}
        for kind, tail in (("published", ""), ("filters", "/filters"))
    ]


def build_enrichment_plan(
    bundle,
    diagnostics=None,
    *,
    dataset_limit=12,
    max_requests=40,
    max_total_bytes=32 * 1024 * 1024,
):
    """Pool development results, then choose exact native candidates deterministically.

    A ranked candidate is not an authenticated paper-use link. Diagnostic content
    is retained by hash for audit; it cannot inject candidates or test questions.
    """
    if not isinstance(bundle, BenchmarkBundle):
        bundle = load_bundle(bundle)
    _integer(dataset_limit, "dataset_limit", 1, 100)
    _integer(max_requests, "max_requests", 1, 1000)
    _integer(max_total_bytes, "max_total_bytes", 1, 256 * 1024 * 1024)
    documents = {row["id"]: row for row in bundle.documents}
    uses, priority, unresolved = {}, Counter(), {}
    methods = ("lexical", "semantic", "hybrid")
    for question in sorted(bundle.questions, key=lambda row: row["id"]):
        if question["split"] != "dev":
            continue
        evidenced = {key for key, grade in question["relevance"].items() if grade > 0}
        for identifier in sorted(evidenced):
            doc = documents[identifier]
            if not _native(doc.get("provider"), doc.get("native_id")):
                item = unresolved.setdefault(
                    identifier,
                    {
                        "dataset_id": identifier,
                        "provider": doc.get("provider"),
                        "native_id": doc.get("native_id"),
                        "reason": "No exact supported native ID; no title-based or historical ID substitution",
                        "question_ids": [],
                        "references": copy.deepcopy(doc["references"]),
                    },
                )
                item["question_ids"].append(question["id"])
            else:
                uses.setdefault(identifier, {})[question["id"]] = {"evidenced_target"}
                priority[identifier] += 100
        for method in methods:
            for position, row in enumerate(rank(bundle, question, method)[:10], 1):
                doc = documents[row["id"]]
                if not _native(doc.get("provider"), doc.get("native_id")):
                    continue
                reasons = uses.setdefault(row["id"], {}).setdefault(question["id"], set())
                reasons.add(f"{method}_top10_candidate")
                priority[row["id"]] += 11 - position
                priority[row["id"]] += sum(c["status"] == "unknown" for c in row["checks"])
    # Alternate providers when both appear; avoid spending the entire bounded run
    # on one source merely because its catalogue contains more near duplicates.
    queues = {
        provider: sorted(
            (identifier for identifier in uses if documents[identifier]["provider"] == provider),
            key=lambda identifier: (-priority[identifier], identifier),
        )
        for provider in ("nomis", "statswales")
    }
    targets, count = [], 0
    while any(queues.values()) and len(targets) < dataset_limit:
        for provider, queue in queues.items():
            if not queue or len(targets) >= dataset_limit:
                continue
            identifier = queue.pop(0)
            doc = documents[identifier]
            requests = _requests(provider, doc["native_id"])
            if count + len(requests) > max_requests:
                continue
            count += len(requests)
            targets.append(
                {
                    "dataset_id": identifier,
                    "provider": provider,
                    "native_id": doc["native_id"],
                    "title": doc["title"],
                    "base_metadata_sha256": _digest(doc),
                    "base_coverage": copy.deepcopy(doc.get("coverage", {})),
                    "question_ids": sorted(uses[identifier]),
                    "reasons": sorted(set().union(*uses[identifier].values())),
                    "references": copy.deepcopy(doc["references"]),
                    "requests": requests,
                }
            )
    if not targets:
        raise EnrichmentError(
            "No development-selected exact native identity fits this request budget"
        )
    plan = {
        "schema_version": 1,
        "kind": "discovery_metadata_enrichment",
        "selection_split": "dev",
        "benchmark": {
            "manifest_sha256": hashlib.sha256(
                (bundle.root / "manifest.json").read_bytes()
            ).hexdigest(),
            **{
                name.replace(".json", "").replace(".npz", "") + "_sha256": hashlib.sha256(
                    (bundle.root / name).read_bytes()
                ).hexdigest()
                for name in ("corpus.json", "questions.json", "embeddings.npz")
            },
        },
        "dataset_limit": dataset_limit,
        "max_requests": max_requests,
        "max_total_bytes": max_total_bytes,
        "planned_requests": count,
        "targets": targets,
        "unresolved": sorted(unresolved.values(), key=lambda row: row["dataset_id"]),
        "diagnostics_sha256": _digest(diagnostics) if diagnostics is not None else None,
        "policy": {
            "selection": "Exact native evidenced targets and pooled top10 development candidates only",
            "candidate_identity": "Ranked candidates do not resolve paper-use or historical-release identity",
            "metadata_only": True,
            "partial_geographies_are_not_exhaustive_places": True,
            "nomis_endpoint_documentation": NOMIS_HELP,
            "unchanged_fields": [
                "concepts",
                "coverage.release",
                "coverage.geography_vintages",
                "coverage.places",
            ],
            "rate_limit": "MetadataClient shared host rate ledger, retry/cooldown and request/byte budgets",
        },
    }
    validate_enrichment_plan(plan)
    return plan


def validate_enrichment_plan(plan):
    """Reject arbitrary URLs, observations, mismatched identities and unbounded plans."""
    if (
        not isinstance(plan, dict)
        or type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 1
        or plan.get("kind") != "discovery_metadata_enrichment"
        or plan.get("selection_split") != "dev"
    ):
        raise EnrichmentError("Expected a development-selected metadata enrichment plan")
    _integer(plan.get("dataset_limit"), "dataset_limit", 1, 100)
    _integer(plan.get("max_requests"), "max_requests", 1, 1000)
    _integer(plan.get("max_total_bytes"), "max_total_bytes", 1, 256 * 1024 * 1024)
    binding = plan.get("benchmark")
    required = {"manifest_sha256", "corpus_sha256", "questions_sha256", "embeddings_sha256"}
    if not isinstance(binding, dict) or set(binding) != required:
        raise EnrichmentError("Plan must bind the exact frozen benchmark")
    if any(not isinstance(value, str) or not SHA.fullmatch(value) for value in binding.values()):
        raise EnrichmentError("Invalid benchmark binding hash")
    targets = plan.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= plan["dataset_limit"]:
        raise EnrichmentError("Invalid bounded target list")
    seen, native_seen, total = set(), set(), 0
    for target in targets:
        if not isinstance(target, dict):
            raise EnrichmentError("Target must be an object")
        identifier = target.get("dataset_id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise EnrichmentError("Invalid or duplicate target identity")
        seen.add(identifier)
        if (
            not isinstance(target.get("base_metadata_sha256"), str)
            or not SHA.fullmatch(target["base_metadata_sha256"])
            or not isinstance(target.get("base_coverage"), dict)
            or not isinstance(target.get("title"), str)
            or not target["title"].strip()
        ):
            raise EnrichmentError("Target must bind its original metadata")
        if (
            not isinstance(target.get("question_ids"), list)
            or not target["question_ids"]
            or any(not isinstance(item, str) or not item for item in target["question_ids"])
            or not isinstance(target.get("references"), list)
            or not target["references"]
        ):
            raise EnrichmentError(
                "Targets require development question IDs and readable references"
            )
        expected = _requests(target.get("provider"), target.get("native_id"))
        native_key = (target["provider"], target["native_id"])
        if native_key in native_seen:
            raise EnrichmentError("Duplicate native provider identity")
        native_seen.add(native_key)
        if target.get("requests") != expected:
            raise EnrichmentError("Plan requests must exactly match metadata-only native endpoints")
        total += len(expected)
    if total > plan["max_requests"] or plan.get("planned_requests") != total:
        raise EnrichmentError("Planned requests exceed or disagree with the request budget")
    _bytes(plan)
    return copy.deepcopy(plan)


def _many(node, label):
    if isinstance(node, dict):
        return [node]
    if isinstance(node, list) and all(isinstance(row, dict) for row in node):
        return node
    raise EnrichmentError(f"Malformed {label}")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _label(row):
    value = row.get("description", row.get("name", ""))
    if isinstance(value, dict):
        return _text(value.get("value"))
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("lang", "en").lower().startswith("en"):
                return _text(item.get("value"))
        return ""
    return _text(value)


def _provenance(source, path, pointer, method="provider literal"):
    return {
        "source_id": source["id"],
        "resource_path": path,
        "json_pointer": pointer,
        "sha256": source["body_sha256"],
        "method": method,
    }


def _set(update, field, value, source, path, pointer, method="provider literal"):
    if value is None or value == "" or value == []:
        return
    if field.startswith("coverage."):
        update["fields"].setdefault("coverage", {})[field.removeprefix("coverage.")] = value
    else:
        update["fields"][field] = value
    update["field_provenance"].setdefault(field, []).append(
        _provenance(source, path, pointer, method)
    )


def _nomis_definition(document, update, source, path):
    try:
        raw = document["structure"]["keyfamilies"]["keyfamily"]
    except (KeyError, TypeError) as exc:
        raise EnrichmentError("Nomis definition lacks keyfamily") from exc
    families = _many(raw, "Nomis keyfamilies")
    if len(families) != 1 or families[0].get("id") != update["native_id"]:
        raise EnrichmentError("Nomis response dataset identity disagrees with target")
    family = families[0]
    pointer = "/structure/keyfamilies/keyfamily" + ("/0" if isinstance(raw, list) else "")
    name = family.get("name")
    if not isinstance(name, dict) or not _text(name.get("value")):
        raise EnrichmentError("Nomis definition has no title")
    description = family.get("description", {})
    if isinstance(description, dict):
        _set(
            update,
            "description",
            _text(description.get("value"))[:2000],
            source,
            path,
            pointer + "/description/value",
            "provider literal; first 2000 characters",
        )
    components = family.get("components", {})
    if not isinstance(components, dict):
        raise EnrichmentError("Malformed Nomis components")
    dimensions = []
    for key in ("dimension", "timedimension"):
        if key not in components:
            continue
        for row in _many(components[key], "Nomis dimensions"):
            concept = _text(row.get("conceptref"))
            if not concept:
                raise EnrichmentError("Nomis dimension lacks conceptref")
            dimensions.append(
                {
                    "name": concept.lower(),
                    "label": concept,
                    "codelist": row.get("codelist"),
                    "values_sample": [],
                    "values_complete": False,
                }
            )
    _set(
        update,
        "variables",
        [row["label"] for row in dimensions],
        source,
        path,
        pointer + "/components",
        "dimension conceptref values",
    )
    _set(
        update,
        "dimensions",
        dimensions,
        source,
        path,
        pointer + "/components",
        "dimension declarations; values unexamined",
    )
    annotations = family.get("annotations", {})
    if not isinstance(annotations, dict):
        raise EnrichmentError("Malformed Nomis annotations")
    entries = _many(annotations.get("annotation", []), "Nomis annotations")
    revisions = {
        row["annotationtitle"]: row.get("annotationtext")
        for row in entries
        if row.get("annotationtitle") in {"LastUpdated", "LastRevised", "Status", "Units"}
    }
    update["provider_revision"] = {
        "kind": "observed_definition_revision",
        "version": family.get("version"),
        "annotations": revisions,
        "historical_revision_addressable": False,
    }
    update["field_provenance"]["provider_revision"] = [
        _provenance(
            source,
            path,
            pointer,
            "definition version and revision annotations; not historical release",
        )
    ]


def _codelists(document):
    try:
        raw = document["structure"]["codelists"]["codelist"]
    except (KeyError, TypeError) as exc:
        raise EnrichmentError("Nomis dimension metadata lacks codelists") from exc
    lists = _many(raw, "Nomis codelists")
    codes, count = [], 0
    for index, codelist in enumerate(lists):
        pointer = "/structure/codelists/codelist" + (f"/{index}" if isinstance(raw, list) else "")
        raw_codes = codelist.get("code", [])
        for offset, code in enumerate(_many(raw_codes, "Nomis codes")):
            count += 1
            if count > MAX_VALUES:
                raise EnrichmentError("Nomis codelist exceeds value budget")
            codes.append(
                (code, pointer + "/code" + (f"/{offset}" if isinstance(raw_codes, list) else ""))
            )
    return codes


def _nomis_codes(document, kind, update, source, path):
    codes = _codelists(document)
    sample = []
    for row, _ in codes[:100]:
        annotations = row.get("annotations", {}).get("annotation", [])
        if not isinstance(annotations, list):
            annotations = [annotations]
        sample.append(
            {
                "reference": str(row.get("value", "")),
                "label": _label(row),
                "type_annotations": {
                    item.get("annotationtitle"): item.get("annotationtext")
                    for item in annotations
                    if isinstance(item, dict)
                    and item.get("annotationtitle") in {"TypeName", "TypeCode", "GeogCode"}
                },
            }
        )
    dimensions = update["fields"].setdefault("dimensions", [])
    dimension = next((item for item in dimensions if item["name"] == kind), None)
    if dimension is None:
        dimension = {"name": kind, "label": kind.upper()}
        dimensions.append(dimension)
    dimension.update(
        {
            "values_sample": sample,
            "declared_value_count": len(codes),
            "values_complete": len(codes) <= 100,
            "scope_partial": kind == "geography",
            "hierarchy_depth": "top level only" if kind == "geography" else "provider codelist",
        }
    )
    update["field_provenance"].setdefault("dimensions", []).append(
        _provenance(
            source, path, "/structure/codelists", "bounded first100 labels; raw codelist retained"
        )
    )
    if kind == "time":
        # Rolling labels such as Apr2004-Mar2005 explicitly span two years.
        parsed = [
            re.findall(
                r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", _label(row) or str(row.get("value", ""))
            )
            for row, _ in codes
        ]
        if parsed and all(1 <= len(set(years)) <= 2 for years in parsed):
            years = [int(year) for values in parsed for year in values]
            _set(
                update,
                "coverage.period_start",
                min(years),
                source,
                path,
                "/structure/codelists",
                "minimum explicit year in all time codelist labels including rolling periods; not release year",
            )
            _set(
                update,
                "coverage.period_end",
                max(years),
                source,
                path,
                "/structure/codelists",
                "maximum explicit year in all time codelist labels; no claim of continuity",
            )
        else:
            update["limitations"].append(
                "Time codelist contains absent or ambiguous years; no observation-period bounds inferred"
            )
    else:
        # Root countries are explicit supported aggregates; this does not say
        # that all descendants or geography types have been enumerated.
        country_names = {
            "England",
            "Wales",
            "Scotland",
            "Northern Ireland",
            "United Kingdom",
            "Great Britain",
            "England and Wales",
        }
        labels = [_label(row) for row, _ in codes]
        country_types = []
        for row, _ in codes:
            annotations = row.get("annotations", {}).get("annotation", [])
            if not isinstance(annotations, list):
                annotations = [annotations]
            country_types.append(
                any(
                    isinstance(item, dict)
                    and item.get("annotationtitle") == "TypeName"
                    and item.get("annotationtext") == "countries"
                    for item in annotations
                )
            )
        if labels and all(country_types) and all(label in country_names for label in labels):
            countries = set(labels)
            if "England and Wales" in countries:
                countries.remove("England and Wales")
                countries.update({"England", "Wales"})
            _set(
                update,
                "coverage.countries",
                sorted(countries),
                source,
                path,
                "/structure/codelists",
                "all root codes explicitly typed countries; literal names; England and Wales expanded",
            )
        update["limitations"].append(
            "Top-level geography hierarchy does not enumerate descendant places, geography levels or vintages; those fields remain unchanged"
        )


def _stats_published(document, update, source, path):
    if not isinstance(document, dict) or document.get("id") != update["native_id"]:
        raise EnrichmentError("StatsWales response dataset identity disagrees with target")
    revision = document.get("published_revision")
    if not isinstance(revision, dict):
        raise EnrichmentError("StatsWales response lacks published_revision")
    metadata = revision.get("metadata", [])
    if not isinstance(metadata, list):
        raise EnrichmentError("StatsWales metadata must be an array")
    for index, row in enumerate(metadata):
        if not isinstance(row, dict):
            raise EnrichmentError("Malformed StatsWales metadata row")
        if _text(row.get("language")).lower() not in {"en", "en-gb"}:
            continue
        _set(
            update,
            "description",
            _text(row.get("summary"))[:2000],
            source,
            path,
            f"/published_revision/metadata/{index}/summary",
            "provider literal; first2000 characters",
        )
        break
    update["provider_revision"] = {
        "kind": "observed_published_revision",
        **{key: revision.get(key) for key in ("id", "revision_index", "publish_at")},
        "historical_revision_addressable": False,
    }
    update["field_provenance"]["provider_revision"] = [
        _provenance(
            source,
            path,
            "/published_revision",
            "published revision metadata; not historical release",
        )
    ]
    for provider_field, coverage_field in (
        ("coverage_start_date", "period_start"),
        ("coverage_end_date", "period_end"),
    ):
        raw = revision.get(provider_field)
        if raw is None:
            continue
        try:
            parsed = date.fromisoformat(raw[:10])
        except (TypeError, ValueError) as exc:
            raise EnrichmentError("Malformed StatsWales declared date coverage") from exc
        _set(
            update,
            "coverage." + coverage_field,
            parsed.year,
            source,
            path,
            "/published_revision/" + provider_field,
            "year from provider-declared ISO date coverage",
        )
    declared = revision.get("coverage")
    if isinstance(declared, dict):
        countries = declared.get("countries")
        if (
            isinstance(countries, list)
            and countries
            and all(isinstance(country, str) and country.strip() for country in countries)
        ):
            _set(
                update,
                "coverage.countries",
                countries,
                source,
                path,
                "/published_revision/coverage/countries",
            )


def _stats_filters(document, update, source, path):
    if not isinstance(document, list):
        raise EnrichmentError("StatsWales filter metadata must be a list")
    dimensions, total = [], 0
    for index, row in enumerate(document):
        if (
            not isinstance(row, dict)
            or not _text(row.get("columnName"))
            or not _text(row.get("factTableColumn"))
            or not isinstance(row.get("values"), list)
        ):
            raise EnrichmentError("Malformed StatsWales dimension declaration")
        sample, count, stack = [], 0, list(reversed(row["values"]))
        while stack:
            node = stack.pop()
            if not isinstance(node, dict) or not isinstance(node.get("reference"), str):
                raise EnrichmentError("Malformed StatsWales dimension value")
            children = node.get("children", [])
            if not isinstance(children, list):
                raise EnrichmentError("Malformed StatsWales dimension hierarchy")
            total += 1
            count += 1
            if total > MAX_VALUES:
                raise EnrichmentError("StatsWales dimension values exceed budget")
            if len(sample) < 100:
                sample.append(
                    {
                        "reference": node["reference"],
                        "label": _text(
                            node.get("description", node.get("label", node.get("name", "")))
                        ),
                    }
                )
            stack.extend(reversed(children))
        dimensions.append(
            {
                "name": row["factTableColumn"],
                "label": row["columnName"],
                "values_sample": sample,
                "declared_value_count": count,
                "values_complete": count <= 100,
            }
        )
        update["field_provenance"].setdefault("dimensions", []).append(
            _provenance(
                source,
                path,
                f"/{index}",
                "declared dimensions and bounded first100 values; raw full hierarchy retained",
            )
        )
        # An entire Area/Country hierarchy rooted at official country codes
        # supplies country coverage without claiming its sample lists all places.
        country_codes = {
            "W92000004": "Wales",
            "E92000001": "England",
            "S92000003": "Scotland",
            "N92000002": "Northern Ireland",
            "K02000001": "United Kingdom",
            "K03000001": "Great Britain",
            "K04000001": "England and Wales",
        }
        roots = row["values"]
        geo_label = row["columnName"].casefold().strip()
        if (
            geo_label
            in {"area", "area name", "country", "country name", "geography", "geography name"}
            and roots
            and all(
                node.get("reference") in country_codes
                and _text(node.get("description", node.get("label", node.get("name", ""))))
                == country_codes[node["reference"]]
                for node in roots
            )
        ):
            countries = {country_codes[node["reference"]] for node in roots}
            if "England and Wales" in countries:
                countries.remove("England and Wales")
                countries.update({"England", "Wales"})
            _set(
                update,
                "coverage.countries",
                sorted(countries),
                source,
                path,
                f"/{index}/values",
                "all geographic roots match explicit ONS country codes and labels; descendants not exhaustive places",
            )
    _set(
        update,
        "variables",
        [row["label"] for row in dimensions],
        source,
        path,
        "",
        "dimension columnName values",
    )
    update["fields"]["dimensions"] = dimensions
    update["limitations"].append(
        "Dimension value samples do not declare exhaustive place coverage or geography vintages"
    )


PARSERS = {
    "definition": _nomis_definition,
    "published": _stats_published,
    "filters": _stats_filters,
}


def _update(target):
    return {
        "dataset_id": target["dataset_id"],
        "provider": target["provider"],
        "native_id": target["native_id"],
        "base_metadata_sha256": target["base_metadata_sha256"],
        "fields": {},
        "field_provenance": {},
        "provider_revision": {},
        "references": [],
        "source_ids": [],
        "limitations": [],
    }


def _parse(target, request, capture, path):
    try:
        document = json.loads(capture.body)
    except (UnicodeError, ValueError) as exc:
        raise EnrichmentError("Provider metadata response is not JSON") from exc
    source = {
        "id": "enrichment-source:"
        + _digest({"request": request, "body_sha256": hashlib.sha256(capture.body).hexdigest()}),
        "title": f"{target['provider']} {request['kind']} metadata for {target['native_id']}",
        "url": capture.metadata.get("url", request["url"]),
        "type": "provider_metadata",
        "resource_paths": sorted(capture.resources(path)),
        "body_sha256": hashlib.sha256(capture.body).hexdigest(),
    }
    update = _update(target)
    try:
        if request["kind"] in {"time", "geography"}:
            _nomis_codes(document, request["kind"], update, source, path)
        else:
            PARSERS[request["kind"]](document, update, source, path)
        _bytes(update)
    except (KeyError, TypeError, AttributeError, RecursionError, ValueError) as exc:
        if isinstance(exc, EnrichmentError):
            raise
        raise EnrichmentError(
            "Malformed provider metadata field types or nonfinite values"
        ) from exc
    return source, update


def _combine(update, part, source):
    for field, value in part["fields"].items():
        if field == "coverage":
            update["fields"].setdefault("coverage", {}).update(value)
        elif field == "dimensions":
            existing = {row["name"]: row for row in update["fields"].get(field, [])}
            for row in value:
                existing[row["name"]] = {**existing.get(row["name"], {}), **row}
            update["fields"][field] = list(existing.values())
        else:
            update["fields"][field] = value
    for field, rows in part["field_provenance"].items():
        update["field_provenance"].setdefault(field, []).extend(rows)
    if part["provider_revision"]:
        update["provider_revision"] = part["provider_revision"]
    update["limitations"].extend(part["limitations"])
    update["source_ids"].append(source["id"])
    update["references"].append({key: source[key] for key in ("id", "title", "url", "type")})


def capture_enrichment(store_root, plan, *, offline=False, refresh=False, client=None):
    """Checkpoint every GET and publish successes plus explicit failure outcomes.

    Interruptions propagate without publication; retained HTTP captures allow
    resumption. Ordinary provider/cache errors yield an inspectable partial
    snapshot. A repeated cache-only run produces the same immutable identity.
    """
    plan = validate_enrichment_plan(plan)
    if offline and refresh:
        raise EnrichmentError("offline and refresh are mutually exclusive")
    if client is None:
        client = MetadataClient(
            store_root, max_requests=plan["max_requests"], max_total_bytes=plan["max_total_bytes"]
        )
    old_requests, old_bytes = client.max_requests, client.max_total_bytes
    client.max_requests = min(
        old_requests if old_requests is not None else plan["max_requests"],
        client.attempts + plan["max_requests"],
    )
    client.max_total_bytes = min(
        old_bytes if old_bytes is not None else plan["max_total_bytes"],
        client.total_bytes + plan["max_total_bytes"],
    )
    before_attempts, before_hits, before_bytes = (
        client.attempts,
        client.cache_hits,
        client.total_bytes,
    )
    resources, sources, outcomes, updates = {}, [], [], []
    try:
        for target in plan["targets"]:
            update = _update(target)
            identity_verified = False
            for request in target["requests"]:
                path = (
                    f"enrichment/{target['provider']}/{target['native_id']}/{request['kind']}.json"
                )
                outcome = {
                    "dataset_id": target["dataset_id"],
                    "provider": target["provider"],
                    "kind": request["kind"],
                    "request": request,
                    "resource_path": path,
                    "status": "failed",
                    "references": [],
                }
                # Child dimension endpoints do not authenticate their parent
                # identity; require successful native definition/publication.
                if request["kind"] not in {"definition", "published"} and not identity_verified:
                    outcome["error"] = (
                        "Dataset identity was not verified; dependent dimension metadata skipped"
                    )
                    outcomes.append(outcome)
                    continue
                try:
                    capture = client.get(
                        request["url"], params=request["params"], offline=offline, refresh=refresh
                    )
                    resources.update(capture.resources(path))
                    source, part = _parse(target, request, capture, path)
                    sources.append(source)
                    _combine(update, part, source)
                    identity_verified = True
                    outcome.update(
                        {
                            "status": "captured",
                            "body_sha256": source["body_sha256"],
                            "source_id": source["id"],
                            "references": [
                                {key: source[key] for key in ("id", "title", "url", "type")}
                            ],
                        }
                    )
                except (RetrievalError, EnrichmentError, ValueError) as exc:
                    outcome["error"] = str(exc)
                outcomes.append(outcome)
            if update["source_ids"]:
                updates.append(update)
    finally:
        client.max_requests, client.max_total_bytes = old_requests, old_bytes
    resources["enrichment-plan.json"] = _bytes(plan) + b"\n"
    plan_source = {
        "id": "enrichment-plan:" + _digest(plan),
        "title": "Frozen development-selected provider metadata enrichment plan",
        "url": "urn:ssdu:benchmark:" + plan["benchmark"]["manifest_sha256"],
        "type": "derivation_plan",
        "resource_paths": ["enrichment-plan.json", "enrichment-derived.json"],
    }
    sources.append(plan_source)
    for outcome in outcomes:
        if not outcome["references"]:
            outcome["references"] = [
                {key: plan_source[key] for key in ("id", "title", "url", "type")}
            ]
    derivation = {
        "schema_version": 1,
        "plan": plan,
        "updates": updates,
        "sources": sources,
        "outcomes": outcomes,
    }
    resources["enrichment-derived.json"] = _bytes(derivation) + b"\n"
    datasets = [
        {
            "id": update["dataset_id"],
            "provider": update["provider"],
            "native_id": update["native_id"],
            "title": target["title"],
            "description": update["fields"].get("description", ""),
            "doi": None,
            "version": None,
            "coverage": update["fields"].get("coverage", {}),
            "source_ids": update["source_ids"],
            "references": update["references"],
        }
        for update in updates
        for target in plan["targets"]
        if update["dataset_id"] == target["dataset_id"]
    ]
    document = {
        "schema_version": 1,
        "sources": sources,
        "papers": [],
        "relationships": [],
        "datasets": datasets,
        "enrichment": derivation,
        "ingestion": {
            "kind": "discovery_metadata_enrichment",
            "plan_sha256": _digest(plan),
            "observations_downloaded": False,
        },
    }
    snapshot_id = LiteratureStore(store_root).publish(document, resources)
    return {
        "snapshot_id": snapshot_id,
        "counts": {
            "datasets": len(updates),
            "sources": len(sources),
            "captured": sum(row["status"] == "captured" for row in outcomes),
            "failed": sum(row["status"] == "failed" for row in outcomes),
        },
        "complete": all(row["status"] == "captured" for row in outcomes),
        "attempts": client.attempts - before_attempts,
        "cache_hits": client.cache_hits - before_hits,
        "downloaded_bytes": client.total_bytes - before_bytes,
        "outcomes": outcomes,
        "offline": offline,
    }


def derive_enrichment(store_root, snapshot_id):
    """Verify and reparse portable raw bytes without touching acquisition caches."""
    store = LiteratureStore(store_root)
    document = store.read(snapshot_id)
    derivation = document.get("enrichment")
    if (
        not isinstance(derivation, dict)
        or document.get("ingestion", {}).get("kind") != "discovery_metadata_enrichment"
    ):
        raise EnrichmentError("Snapshot is not metadata enrichment")
    root = store.path(snapshot_id) / "resources"
    plan = validate_enrichment_plan(json.loads((root / "enrichment-plan.json").read_bytes()))
    if plan != derivation.get("plan") or _digest(plan) != document["ingestion"].get("plan_sha256"):
        raise EnrichmentError("Retained plan binding disagrees with enrichment document")
    if json.loads((root / "enrichment-derived.json").read_bytes()) != derivation:
        raise EnrichmentError("Retained normalized derivation disagrees with enrichment document")
    sources, updates, outcomes = [], [], derivation.get("outcomes", [])
    if not isinstance(outcomes, list) or len(outcomes) != plan["planned_requests"]:
        raise EnrichmentError("Outcome list disagrees with the frozen request plan")
    by_key = {(row.get("dataset_id"), row.get("kind")): row for row in outcomes}
    if len(by_key) != len(outcomes):
        raise EnrichmentError("Duplicate request outcome")
    for target in plan["targets"]:
        update, identity_verified = _update(target), False
        for request in target["requests"]:
            outcome = by_key.get((target["dataset_id"], request["kind"]))
            path = f"enrichment/{target['provider']}/{target['native_id']}/{request['kind']}.json"
            if (
                outcome is None
                or outcome.get("request") != request
                or outcome.get("resource_path") != path
            ):
                raise EnrichmentError("Retained outcome request does not match the frozen plan")
            if outcome.get("status") == "failed":
                if (root / path).exists():
                    capture = _retained_capture(root, path, request)
                    try:
                        _parse(target, request, capture, path)
                    except EnrichmentError as exc:
                        if str(exc) != outcome.get("error"):
                            raise EnrichmentError(
                                "Retained rejected metadata reason disagrees"
                            ) from exc
                    else:
                        raise EnrichmentError("Retained rejected metadata parses successfully")
                continue
            if outcome.get("status") != "captured" or (
                request["kind"] not in {"definition", "published"} and not identity_verified
            ):
                raise EnrichmentError("Captured dimensions lack verified dataset identity")
            source, part = _parse(target, request, _retained_capture(root, path, request), path)
            if (
                outcome.get("source_id") != source["id"]
                or outcome.get("body_sha256") != source["body_sha256"]
            ):
                raise EnrichmentError("Retained source binding disagrees")
            sources.append(source)
            _combine(update, part, source)
            identity_verified = True
        if update["source_ids"]:
            updates.append(update)
    plan_sources = [
        row for row in derivation.get("sources", []) if row.get("type") == "derivation_plan"
    ]
    if len(plan_sources) != 1:
        raise EnrichmentError("Missing frozen plan reference")
    sources.extend(plan_sources)
    replay = {
        "schema_version": 1,
        "plan": plan,
        "updates": updates,
        "sources": sources,
        "outcomes": outcomes,
    }
    if replay != derivation:
        raise EnrichmentError("Raw metadata replay disagrees with normalized updates")
    return {**replay, "snapshot_id": snapshot_id}


def _retained_capture(root, path, request):
    metadata = json.loads((root / (path + ".capture.json")).read_bytes())
    body = (root / path).read_bytes()
    identity = {
        key: value for key, value in metadata.items() if key not in {"capture_id", "captured_at"}
    }
    if (
        metadata.get("request") != {"url": request["url"], "params": request["params"]}
        or metadata.get("body_sha256") != hashlib.sha256(body).hexdigest()
        or metadata.get("bytes") != len(body)
        or metadata.get("capture_id") != _digest(identity)
    ):
        raise EnrichmentError("Retained capture request or body binding disagrees")
    return Capture(body, metadata, True)
