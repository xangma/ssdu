"""Bounded linked-data cube retrieval from statistics.gov.scot.

Fixed SELECT templates retain complete selected observation triples and labels in
one response. Dataset and data-structure metadata bracket that response. This
checks observed metadata stability, not an unavailable historical revision pin.
File/ZIP datasets and slice-attached components are deliberately unsupported.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

import pandas as pd

from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError

ENDPOINT = "https://statistics.gov.scot/sparql.json"
BASE = "http://statistics.gov.scot/data/"
QB = "http://purl.org/linked-data/cube#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
DCT = "http://purl.org/dc/terms/"
AREA = "http://purl.org/linked-data/sdmx/2009/dimension#refArea"
PERIOD = "http://purl.org/linked-data/sdmx/2009/dimension#refPeriod"
UNIT = "http://purl.org/linked-data/sdmx/2009/attribute#unitMeasure"
STATUS = "http://purl.org/linked-data/sdmx/2009/attribute#obsStatus"
MAX_METADATA_ROWS = 1000
MAX_CELLS = 200_000
_URI = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")


def default_request() -> Request:
    """Small retained Scottish national example; provider marks it discontinued."""
    return Request(
        "scotland",
        "child-dental-health",
        {
            "dimensions": {
                AREA: ["http://statistics.gov.scot/id/statistical-geography/S92000003"],
                PERIOD: ["http://reference.data.gov.uk/id/year/2018"],
            }
        },
        page_size=100,
        max_rows=100,
    )


def _uri(value):
    if not isinstance(value, str) or len(value) > 2048 or not _URI.fullmatch(value):
        raise RetrievalError("Scotland selectors must be absolute HTTP(S) URI identifiers")
    return value


def validate_request(request: Request) -> None:
    if request.provider != "scotland":
        raise RetrievalError("Scotland adapter received a different provider")
    if not isinstance(request.dataset_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,159}", request.dataset_id
    ):
        raise RetrievalError("Scotland dataset_id must be a native dataset slug")
    if not isinstance(request.params, dict):
        raise RetrievalError("Scotland params must be an object")
    unknown = set(request.params) - {"dimensions", "expected_modified"}
    if unknown:
        raise RetrievalError(f"Unsupported Scotland parameters: {sorted(unknown)}")
    dimensions = request.params.get("dimensions", {})
    if not isinstance(dimensions, dict) or len(dimensions) > 32:
        raise RetrievalError("Scotland dimensions must be an object of at most 32 selectors")
    for dimension, values in dimensions.items():
        _uri(dimension)
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 100
            or any(not isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
        ):
            raise RetrievalError("Scotland dimension selectors need 1–100 distinct URI codes")
        for value in values:
            _uri(value)
    modified = request.params.get("expected_modified")
    if modified is not None and (not isinstance(modified, str) or not 1 <= len(modified) <= 100):
        raise RetrievalError("Scotland expected_modified must be a nonempty metadata timestamp")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _bindings(resource, required):
    payload = resource.json()
    try:
        variables = payload["head"]["vars"]
        rows = payload["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise RetrievalError("Malformed Scotland SPARQL SELECT result") from exc
    if (
        not isinstance(variables, list)
        or any(not isinstance(value, str) for value in variables)
        or len(set(variables)) != len(variables)
        or not set(required).issubset(variables)
        or not isinstance(rows, list)
    ):
        raise RetrievalError("Malformed Scotland SPARQL result variables or bindings")
    for row in rows:
        if (
            not isinstance(row, dict)
            or not set(required).issubset(row)
            or set(row) - set(variables)
        ):
            raise RetrievalError("Scotland SPARQL row is missing required bindings")
        for term in row.values():
            if (
                not isinstance(term, dict)
                or term.get("type") not in {"uri", "literal", "typed-literal"}
                or not isinstance(term.get("value"), str)
                or set(term) - {"type", "value", "datatype", "xml:lang"}
                or any(not isinstance(value, str) for value in term.values())
            ):
                raise RetrievalError("Unsupported Scotland RDF term")
            if term["type"] == "uri" and ("datatype" in term or "xml:lang" in term):
                raise RetrievalError("Unsupported Scotland RDF URI term annotations")
        if "label" in row and row["label"]["type"] not in {"literal", "typed-literal"}:
            raise RetrievalError("Scotland labels must be RDF literals")
    return rows


def metadata_query(dataset_id):
    """Deterministic, bounded dataset and DSD interpretation dependency query."""
    return f"""SELECT DISTINCT ?kind ?property ?value ?label ?attachment ?required WHERE {{
{{ BIND("dataset" AS ?kind) <{BASE}{dataset_id}> ?property ?value }}
UNION {{
<{BASE}{dataset_id}> <{QB}structure> ?dsd .
?dsd <{QB}component> ?component .
?component ?role ?property .
VALUES ?role {{ <{QB}dimension> <{QB}measure> <{QB}attribute> }}
BIND(STR(?role) AS ?kind) BIND(?property AS ?value)
OPTIONAL {{ ?property <{RDFS}label> ?label FILTER(LANG(?label) = "" || LANGMATCHES(LANG(?label), "en")) }}
OPTIONAL {{ ?component <{QB}componentAttachment> ?attachment }}
OPTIONAL {{ ?component <{QB}componentRequired> ?required }}
}}
}} ORDER BY ?kind ?property ?value ?label LIMIT {MAX_METADATA_ROWS + 1}"""


def observations_query(request):
    selections = []
    for index, (dimension, values) in enumerate(
        sorted(request.params.get("dimensions", {}).items())
    ):
        variable = f"?selected{index}"
        selections.append(
            f"?obs <{dimension}> {variable} . VALUES {variable} {{ "
            + " ".join(f"<{value}>" for value in sorted(values))
            + " }"
        )
    selected = "\n".join(selections)
    cell_limit = min(MAX_CELLS, max(1000, request.max_rows * 64))
    return f"""SELECT DISTINCT ?obs ?property ?value ?label WHERE {{
{{ SELECT DISTINCT ?obs WHERE {{
?obs <{QB}dataSet> <{BASE}{request.dataset_id}> .
{selected}
}} ORDER BY ?obs LIMIT {request.max_rows + 1} }}
?obs ?property ?value .
OPTIONAL {{ ?value <{RDFS}label> ?label FILTER(LANG(?label) = "" || LANGMATCHES(LANG(?label), "en")) }}
}} ORDER BY ?obs ?property ?value ?label LIMIT {cell_limit + 1}"""


def _structure(rows):
    if len(rows) > MAX_METADATA_ROWS:
        raise RetrievalError("Scotland metadata exceeds bounded interpretation query")
    properties = {}
    dataset = defaultdict(list)
    for row in rows:
        if row["property"]["type"] != "uri":
            raise RetrievalError("Scotland property identifiers must be URIs")
        kind, prop, value = (row[key]["value"] for key in ("kind", "property", "value"))
        if kind == "dataset":
            dataset[prop].append(value)
            continue
        if kind not in {QB + "dimension", QB + "measure", QB + "attribute"} or value != prop:
            raise RetrievalError("Unsupported Scotland data structure component")
        attachment = row.get("attachment", {}).get("value")
        if attachment not in {None, QB + "Observation", QB + "DataSet"}:
            raise RetrievalError("Scotland slice-attached components are unsupported")
        if attachment == QB + "DataSet" and kind != QB + "attribute":
            raise RetrievalError("Scotland dataset-attached dimensions/measures are unsupported")
        item = {
            "uri": prop,
            "kind": kind.removeprefix(QB),
            "label": row.get("label", {}).get("value"),
            "attachment": attachment,
            "required": row.get("required", {}).get("value") in {"true", "1"},
        }
        if prop in properties and properties[prop] != item:
            raise RetrievalError("Ambiguous Scotland component definition or labels")
        properties[prop] = item
    if len(set(dataset.get(QB + "structure", []))) != 1 or not any(
        v["kind"] == "measure" for v in properties.values()
    ):
        raise RetrievalError(
            "Scotland dataset is not a supported linked-data observation cube; file datasets are unsupported"
        )
    if not any(v["kind"] == "dimension" for v in properties.values()) or len(properties) > 64:
        raise RetrievalError("Scotland cube requires declared dimensions and at most 64 components")
    return {"properties": properties, "dataset": {k: sorted(set(v)) for k, v in dataset.items()}}


def _single(dataset, prop, *, default=None):
    values = dataset.get(prop, [])
    if len(values) > 1:
        raise RetrievalError(f"Ambiguous Scotland dataset metadata: {prop}")
    return values[0] if values else default


def _frame(rows, structure, request):
    limit = min(MAX_CELLS, max(1000, request.max_rows * 64))
    if len(rows) > limit:
        raise RetrievalError(
            "Scotland observation triples exceed cell bound; selection may be incomplete"
        )
    observations = defaultdict(lambda: defaultdict(dict))
    labels = defaultdict(set)
    for row in rows:
        if row["obs"]["type"] != "uri" or row["property"]["type"] != "uri":
            raise RetrievalError("Scotland observation/property identities must be URIs")
        obs, prop = row["obs"]["value"], row["property"]["value"]
        term = row["value"]
        key = json.dumps(term, sort_keys=True)
        observations[obs][prop][key] = term
        if row.get("label"):
            labels[term["value"]].add(row["label"]["value"])
    if len(observations) > request.max_rows:
        raise RetrievalError(f"Scotland result exceeds max_rows={request.max_rows}")
    columns = {"observation_uri", QB + "dataSet", *structure["properties"]}
    columns.update(prop for item in observations.values() for prop in item)
    if len(columns) > 128:
        raise RetrievalError("Scotland observations exceed source-column bound")
    records = []
    for obs, item in sorted(observations.items()):
        source = {"observation_uri": obs}
        for prop, terms in item.items():
            values = list(terms.values())
            if prop == QB + "dataSet" and values != [
                {"type": "uri", "value": BASE + request.dataset_id}
            ]:
                raise RetrievalError("Scotland observation identifies a different dataset")
            definition = structure["properties"].get(prop)
            if definition and len(values) != 1:
                raise RetrievalError("Scotland observation component has multiple RDF values")
            if definition and definition["kind"] == "dimension" and values[0]["type"] != "uri":
                raise RetrievalError("Scotland dimensions must use URI member codes")
            source[prop] = (
                values[0]["value"] if len(values) == 1 else json.dumps(values, sort_keys=True)
            )
        if source.get(QB + "dataSet") != BASE + request.dataset_id:
            raise RetrievalError("Scotland observation is missing its dataset identity")
        for prop, definition in structure["properties"].items():
            if prop not in source and definition["attachment"] == QB + "DataSet":
                source[prop] = _single(structure["dataset"], prop)
            if (definition["kind"] == "dimension" or definition["required"]) and source.get(
                prop
            ) is None:
                raise RetrievalError(
                    "Scotland observation is missing a required component; incomplete selection"
                )
        for prop, selected in request.params.get("dimensions", {}).items():
            if source.get(prop) not in selected:
                raise RetrievalError("Scotland response violates requested dimension selection")
        records.append(source)
    frame = pd.DataFrame(
        records, columns=["observation_uri", *sorted(columns - {"observation_uri"})], dtype="string"
    )
    return frame, {
        code: next(iter(values)) if len(values) == 1 else None
        for code, values in sorted(labels.items())
    }


def search(query: str, client: HttpClient) -> list[dict]:
    """At most 25 linked cube titles; intentionally not an exhaustive catalogue."""
    if not isinstance(query, str) or len(query) > 200:
        raise RetrievalError("Scotland search must be at most 200 characters")
    literal = json.dumps(query, ensure_ascii=True)
    sparql = f"""SELECT DISTINCT ?dataset ?title WHERE {{
?dataset <{QB}structure> ?structure ; <{DCT}title> ?title .
FILTER(CONTAINS(LCASE(STR(?title)), LCASE({literal})))
}} ORDER BY ?dataset ?title LIMIT 25"""
    rows = _bindings(client.get(ENDPOINT, params={"query": sparql}), {"dataset", "title"})
    if len(rows) > 25:
        raise RetrievalError("Scotland catalogue exceeds requested bound")
    result = []
    for row in rows:
        uri = row["dataset"]["value"]
        if row["dataset"]["type"] != "uri" or not uri.startswith(BASE):
            raise RetrievalError("Scotland catalogue contains unsupported dataset identity")
        identity = uri.removeprefix(BASE)
        validate_request(Request("scotland", identity))
        result.append({"id": identity, "title": row["title"]["value"], "provider": "scotland"})
    return result


def fetch(request: Request, client: HttpClient) -> Acquisition:
    validate_request(request)
    before = _bindings(
        client.get(ENDPOINT, params={"query": metadata_query(request.dataset_id)}),
        {"kind", "property", "value"},
    )
    structure = _structure(before)
    for prop in request.params.get("dimensions", {}):
        if structure["properties"].get(prop, {}).get("kind") != "dimension":
            raise RetrievalError(f"Unknown Scotland dimension selector: {prop}")
    modified = _single(structure["dataset"], DCT + "modified")
    if request.params.get("expected_modified") not in {None, modified}:
        raise RetrievalError("Scotland modified timestamp does not match requested release")
    rows = _bindings(
        client.get(ENDPOINT, params={"query": observations_query(request)}),
        {"obs", "property", "value"},
    )
    frame, labels = _frame(rows, structure, request)
    after = _bindings(
        client.get(ENDPOINT, params={"query": metadata_query(request.dataset_id)}),
        {"kind", "property", "value"},
    )
    # RDF set order is irrelevant; preserve all distinct interpretation terms.
    canonical = lambda values: sorted({_digest(row) for row in values})
    if canonical(before) != canonical(after):
        raise RetrievalError("Scotland dataset metadata or structure changed during retrieval")
    title = _single(
        structure["dataset"],
        DCT + "title",
        default=_single(structure["dataset"], RDFS + "label", default=request.dataset_id),
    )
    acquisition = Acquisition(
        provider="scotland",
        dataset_id=request.dataset_id,
        title=title,
        frame=frame,
        metadata={
            "structure": structure,
            "member_labels": labels,
            "effective_query": request.to_dict(),
            "provider_total_rows": len(frame),
            "complete_selection": True,
            "query_strategy": "single_atomic_observation_response_bracketed_by_metadata",
            "revision_limitations": "No immutable upstream revision pin; matching metadata checks bracket one atomic observation query.",
            "source_references": [
                "https://guides.statistics.gov.scot/article/22-querying-data-with-sparql",
                "https://guides.statistics.gov.scot/article/41-api-access-for-file-datasets-not-linked-data",
            ],
        },
        release={
            "modified": modified,
            "issued": _single(structure["dataset"], DCT + "issued"),
            "metadata_sha256": _digest(canonical(before)),
            "revision_status": "metadata_checked_unpinned",
        },
        licence=_single(
            structure["dataset"], DCT + "license", default="Unknown; consult source metadata"
        ),
        citation=f"{title}. statistics.gov.scot. {BASE}{request.dataset_id}",
        source_url=BASE + request.dataset_id,
    )
    from ssdu.normalization_scotland import normalize

    acquisition.dataset = normalize(acquisition, request=request)
    return acquisition
