"""Pure, source-preserving transforms into the versioned observation contract.

No network or filesystem access occurs here. Provider tables remain available
as ``source``; observations preserve source strings separately from numbers.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from ssdu.dataset_contract import Dataset, DatasetContractError
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.providers import get_provider, provider_ids

TRANSFORM_VERSION = "1.0.0"
TRANSFORM_IDS = {provider: get_provider(provider).transform_id for provider in provider_ids()}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_GROUPED = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_RESERVED = {"measure", "value", "value_raw", "unit", "status", "notes"}


def _dtype(value):
    if pa.types.is_float64(value):
        return "float64"
    if pa.types.is_date32(value):
        return "date32"
    return str(value)


def _text(value: Any) -> str | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return str(value)


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not result or result in _RESERVED:
        raise RetrievalError(f"Source dimension has an unsupported normalized name: {value}")
    return result


def _code(value):
    text = _text(value)
    return text if text is not None and text.strip() else None


def _number(value: Any) -> float | None:
    text = _text(value)
    if text is None or not text.strip() or isinstance(value, bool):
        return None
    candidate = text.strip()
    if _GROUPED.fullmatch(candidate):
        candidate = candidate.replace(",", "")
    if not _NUMBER.fullmatch(candidate):
        return None
    numeric = float(candidate)
    return numeric if math.isfinite(numeric) else None


def _diagnostic(code, message, column=None, count=None):
    return {"code": code, "message": message, "column": column, "count": count}


def _codes(values, labels=None):
    labels = labels if labels is not None else [None] * len(values)
    found: dict[str, set[str]] = defaultdict(set)
    for value, label in zip(values, labels, strict=True):
        if value is not None:
            found[value]
            if label is not None:
                found[value].add(label)
    return [
        {"code": code, "label": next(iter(names)) if len(names) == 1 else None}
        for code, names in sorted(found.items())
    ]


def _dimension(column, label, values, labels=None, *, kind="category", level=None, vintage=None):
    return {
        "id": column,
        "column": column,
        "label": label,
        "kind": kind,
        "codes": _codes(values, labels),
        "nullable": any(value is None for value in values),
        "level": level,
        "vintage": vintage,
    }


def _checked_frame(acquisition):
    frame = acquisition.frame
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise RetrievalError("Normalization requires a table with unique source column names")
    if any(not isinstance(column, str) for column in frame.columns):
        raise RetrievalError("Normalization requires string source column names")
    return frame.reset_index(drop=True)


def _build(acquisition, request, observations, dimensions, variables, diagnostics):
    frame = _checked_frame(acquisition)
    columns = [dim["column"] for dim in dimensions]
    if len(set(columns)) != len(columns):
        raise RetrievalError("Source dimensions create ambiguous normalized column names")
    if any(column in _RESERVED for column in columns):
        raise RetrievalError("Source dimension collides with a required observation column")
    names = [*columns, "measure", "value", "value_raw", "unit", "status", "notes"]
    schema = pa.schema(
        [pa.field(name, pa.float64() if name == "value" else pa.string()) for name in names]
    )
    try:
        table = (
            observations.select(names).cast(schema)
            if isinstance(observations, pa.Table)
            else pa.Table.from_pylist(observations, schema=schema)
        )
        source = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(None)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError) as exc:
        raise RetrievalError(
            "Source fields cannot be represented losslessly by the dataset contract"
        ) from exc
    role = {name: name for name in _RESERVED}
    table_columns = [
        {
            "name": name,
            "dtype": _dtype(table.schema.field(name).type),
            "role": "dimension" if name in columns else role[name],
            "nullable": name != "measure"
            and (
                name not in columns
                or next(d["nullable"] for d in dimensions if d["column"] == name)
            ),
        }
        for name in names
    ]
    source_columns = [
        {"name": field.name, "dtype": _dtype(field.type), "role": "attribute", "nullable": True}
        for field in source.schema
    ]
    for dim in dimensions:
        missing = table.column(dim["column"]).null_count
        if missing:
            diagnostics.append(
                _diagnostic(
                    "unknown_dimension",
                    "Source codes are missing or ambiguous; labels were not substituted for codes",
                    dim["column"],
                    missing,
                )
            )
    invalid = (
        pc.sum(
            pc.fill_null(
                pc.and_(
                    pc.greater(pc.utf8_length(pc.utf8_trim_whitespace(table["value_raw"])), 0),
                    pc.is_null(table["value"]),
                ),
                False,
            )
        ).as_py()
        or 0
    )
    if invalid:
        diagnostics.append(
            _diagnostic(
                "non_numeric_value",
                "Uninterpreted source tokens retained in value_raw; numeric values remain null",
                "value",
                invalid,
            )
        )
    total = acquisition.metadata.get(
        "provider_total_rows", acquisition.metadata.get("provider_row_count")
    )
    complete = acquisition.metadata.get("complete_selection") is True or (
        isinstance(total, int) and not isinstance(total, bool) and total == len(frame)
    )
    descriptor = {
        "contract_version": "1.0.0",
        "profile": "observations",
        "dataset_id": f"{acquisition.provider}:{acquisition.dataset_id}",
        "title": acquisition.title,
        "source": {
            "provider": acquisition.provider,
            "dataset_id": acquisition.dataset_id,
            "url": acquisition.source_url,
            "licence": acquisition.licence,
            "citation": acquisition.citation,
            "release": acquisition.release,
            "effective_request": request.to_dict()
            if request is not None
            else acquisition.metadata.get("effective_query", {}),
            "raw_inputs": [],
            "parent_snapshot_ids": [],
        },
        "transform": {
            "id": get_provider(acquisition.provider).transform_id,
            "version": get_provider(acquisition.provider).transform_version,
        },
        "dimensions": dimensions,
        "variables": sorted(variables, key=lambda v: v["id"]),
        "tables": [
            {
                "name": "observations",
                "path": "tables/observations.parquet",
                "rows": len(observations),
                "columns": table_columns,
            },
        ]
        + (
            [
                {
                    "name": "source",
                    "path": "tables/source.parquet",
                    "rows": len(frame),
                    "columns": source_columns,
                }
            ]
            if source_columns
            else []
        ),
        "quality": {
            "source_rows": len(frame),
            "observation_rows": len(observations),
            "completeness": "complete" if complete else "unknown",
            "diagnostics": diagnostics,
        },
        "coverage": {
            "geography": {
                "status": "observed"
                if any(d["kind"] == "geography" for d in dimensions)
                else "unknown",
                "dimensions": [d["column"] for d in dimensions if d["kind"] == "geography"],
            },
            "temporal": {
                "status": "observed" if any(d["kind"] == "time" for d in dimensions) else "unknown",
                "dimensions": [d["column"] for d in dimensions if d["kind"] == "time"],
            },
            "population": None,
        },
    }
    try:
        return Dataset.from_tables(
            descriptor, {"observations": table, **({"source": source} if source_columns else {})}
        )
    except DatasetContractError as exc:
        raise RetrievalError(f"Source does not satisfy the observation contract: {exc}") from exc


def _variable(identity, label, source_column, *, unit=None, kind="measure", description=None):
    return {
        "id": identity,
        "label": label,
        "source_column": source_column,
        "unit": unit,
        "kind": kind,
        "description": description,
    }


def nomis(acquisition: Acquisition, request: Request | None = None) -> Dataset:
    """Preserve Nomis dimension codes and independent MEASURE/MEASURES identities."""
    frame = _checked_frame(acquisition)
    if not {"OBS_VALUE", "OBS_STATUS"}.issubset(frame.columns):
        raise RetrievalError("Unsupported Nomis observation schema")
    definition = acquisition.metadata.get("definition", {})
    try:
        families = definition["structure"]["keyfamilies"]["keyfamily"]
        family = families[0] if isinstance(families, list) else families
        native = family["components"].get("dimension", [])
    except (KeyError, TypeError, IndexError) as exc:
        raise RetrievalError(
            "Nomis normalization requires the retained dataset definition"
        ) from exc
    native = [native] if isinstance(native, dict) else native
    concepts = [dim.get("conceptref") for dim in native if isinstance(dim, dict)]
    if any(not isinstance(concept, str) for concept in concepts) or len(set(concepts)) != len(
        concepts
    ):
        raise RetrievalError("Nomis definition has ambiguous dimension concepts")
    diagnostics, dimensions, values = [], [], {}
    observed = ["DATE"] if "DATE" in frame or "DATE_CODE" in frame else []
    observed.extend(
        concept
        for concept in concepts
        if concept not in {"MEASURE", "MEASURES", "TIME", "DATE"}
        and (concept in frame or concept + "_CODE" in frame)
    )
    for concept in observed:
        name = "period" if concept == "DATE" else _slug(concept)
        code = concept + "_CODE" if concept + "_CODE" in frame else concept
        data = [_code(value) for value in frame[code]]
        labels = (
            [_text(value) for value in frame[concept + "_NAME"]]
            if concept + "_NAME" in frame
            else None
        )
        levels = set(frame[concept + "_TYPE"].dropna()) if concept + "_TYPE" in frame else set()
        dimensions.append(
            _dimension(
                name,
                concept,
                data,
                labels,
                kind="time"
                if concept == "DATE"
                else "geography"
                if concept == "GEOGRAPHY"
                else "category",
                level=next(iter(levels)) if len(levels) == 1 and concept == "GEOGRAPHY" else None,
            )
        )
        values[name] = data
    if not dimensions and len(frame):
        raise RetrievalError("Nomis observation schema has no source-bound dimensions")
    for concept in concepts:
        if concept not in {"MEASURE", "MEASURES", "TIME", "DATE"} and concept not in observed:
            diagnostics.append(
                _diagnostic(
                    "unobserved_dimension",
                    "Provider definition declares a dimension omitted from the returned cells; provider defaults remain unexpanded",
                    concept,
                )
            )
    annotations = family.get("annotations", {}).get("annotation", [])
    annotations = [annotations] if isinstance(annotations, dict) else annotations
    units = {
        str(a["annotationtext"])
        for a in annotations
        if isinstance(a, dict) and a.get("annotationtitle") == "Units" and a.get("annotationtext")
    }
    default_unit = next(iter(units)) if len(units) == 1 else None
    observations, variables, unknown_units = [], {}, 0
    for index, row in frame.iterrows():
        identities, labels = [], []
        for concept in ("MEASURE", "MEASURES"):
            code = concept + "_CODE" if concept + "_CODE" in frame else concept
            if code in frame:
                value = _text(row[code])
                if value is None or not value:
                    raise RetrievalError("Nomis measure identity is missing")
                identities.append(f"{concept}={value}")
                labels.append(_text(row.get(concept + "_NAME")) or value)
        identity = "|".join(identities) or "OBS_VALUE"
        raw, status = _text(row["OBS_VALUE"]), _text(row["OBS_STATUS"])
        unit = _text(row.get("UNIT_NAME")) or _text(row.get("UNIT"))
        if unit is None and (
            "MEASURES_NAME" not in frame or _text(row["MEASURES_NAME"]) == "Value"
        ):
            unit = default_unit
        if unit is None:
            unknown_units += 1
        variable = _variable(
            identity, " / ".join(labels) or acquisition.title, "OBS_VALUE", unit=unit
        )
        if identity in variables and variables[identity]["label"] != variable["label"]:
            raise RetrievalError("Nomis measure code has conflicting source labels")
        if identity in variables and variables[identity]["unit"] != unit:
            variable["unit"] = None
        variables[identity] = variable
        observations.append(
            {
                **{name: data[index] for name, data in values.items()},
                "measure": identity,
                "value": _number(raw),
                "value_raw": raw,
                "unit": unit,
                "status": status or ("missing" if not raw or not raw.strip() else "unclassified"),
                "notes": _text(row.get("OBS_STATUS_NAME")),
            }
        )
    if unknown_units:
        diagnostics.append(
            _diagnostic(
                "unknown_unit",
                "No unambiguous observation unit is declared by retained source metadata",
                "unit",
                unknown_units,
            )
        )
    return _build(
        acquisition, request, observations, dimensions, list(variables.values()), diagnostics
    )


def _wales_nodes(nodes):
    if not isinstance(nodes, list):
        raise RetrievalError("StatsWales normalization requires explicit dimension values")
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("reference"), str):
            raise RetrievalError("StatsWales normalization has an invalid reference code")
        yield node
        if "children" in node:
            yield from _wales_nodes(node["children"])


def statswales(acquisition: Acquisition, request: Request | None = None) -> Dataset:
    """Use explicit filter references; never turn an ambiguous label into a code."""
    frame = _checked_frame(acquisition)
    native = acquisition.metadata.get("dimensions")
    if not isinstance(native, list) or any(not isinstance(dim, dict) for dim in native):
        raise RetrievalError("StatsWales normalization requires retained filter metadata")
    candidates = [
        name
        for name in ("Data values", "Data value", "Data Values", "Gwerthoedd data")
        if name in frame
    ]
    if len(candidates) != 1 and len(frame):
        raise RetrievalError("Unsupported or ambiguous StatsWales data-value schema")
    value_column = candidates[0] if candidates else "Data values"
    dimensions, values, diagnostics = [], {}, []
    variable_dimension = None
    native_names = set()
    for dim in native:
        column = dim.get("columnName")
        if not isinstance(column, str) or not isinstance(dim.get("factTableColumn"), str):
            raise RetrievalError("StatsWales normalization has an invalid dimension column")
        name = _slug(column)
        if name in native_names:
            raise RetrievalError("StatsWales dimensions create ambiguous normalized column names")
        native_names.add(name)
        mapping = defaultdict(set)
        for node in _wales_nodes(dim.get("values")):
            if isinstance(node.get("description"), str):
                mapping[node["description"].strip()].add(node["reference"])
        labels = (
            [_text(value) for value in frame[column]] if column in frame else [None] * len(frame)
        )
        data = [
            next(iter(mapping[label.strip()]))
            if label is not None and len(mapping.get(label.strip(), set())) == 1
            else None
            for label in labels
        ]
        # A recovered code column is convenience evidence, never independent authority.
        recovered = column + "__code"
        if recovered in frame and any(
            _text(value) != code for value, code in zip(frame[recovered], data, strict=True)
        ):
            raise RetrievalError(
                "StatsWales recovered codes disagree with retained filter metadata"
            )
        internal = dim["factTableColumn"].casefold()
        if internal in {"measureid", "measure_id"} and all(code is not None for code in data):
            if variable_dimension is not None:
                raise RetrievalError("StatsWales metadata declares ambiguous measure dimensions")
            variable_dimension = (column, data, labels)
            continue
        if internal in {"measureid", "measure_id"}:
            diagnostics.append(
                _diagnostic(
                    "unknown_measure",
                    "Measure reference codes are ambiguous or missing; source measure labels remain a dimension and variable meaning is unresolved",
                    name,
                    sum(code is None for code in data),
                )
            )
        kind = (
            "time"
            if internal in {"yearid", "timeid", "dateid", "periodid"}
            else "geography"
            if internal in {"areaid", "geographyid", "regionid", "localauthorityid"}
            else "category"
        )
        dimensions.append(_dimension(name, column, data, labels, kind=kind))
        values[name] = data
    if len(set(values)) != len(dimensions):
        raise RetrievalError("StatsWales dimensions create ambiguous normalized column names")
    observations, variables = [], {}
    for index, row in frame.iterrows():
        raw = _text(row[value_column])
        notes = _text(row.get("Notes", row.get("Nodiadau")))
        number = _number(raw)
        status = (
            "missing"
            if raw is None or not raw.strip()
            else "numeric"
            if number is not None
            else "non_numeric"
        )
        measure = variable_dimension[1][index] if variable_dimension else value_column
        label = variable_dimension[2][index] if variable_dimension else acquisition.title
        variable = _variable(measure, label, value_column)
        if measure in variables and variables[measure]["label"] != label:
            raise RetrievalError("StatsWales measure code has conflicting source labels")
        variables[measure] = variable
        observations.append(
            {
                **{name: data[index] for name, data in values.items()},
                "measure": measure,
                "value": number,
                "value_raw": raw,
                "unit": None,
                "status": status,
                "notes": notes,
            }
        )
    diagnostics.append(
        _diagnostic(
            "unknown_unit",
            "The retained StatsWales API metadata does not declare an unambiguous unit; no unit inferred from the title",
            "unit",
            len(frame),
        )
    )
    return _build(
        acquisition,
        request,
        observations,
        dimensions,
        list(variables.values()) or [_variable(value_column, acquisition.title, value_column)],
        diagnostics,
    )


def deprivation(
    acquisition: Acquisition, request: Request | None = None, raw_frame: pd.DataFrame | None = None
) -> Dataset:
    """Melt every File 7 measure, retaining exact source tokens when available."""
    frame = _checked_frame(acquisition)
    column_map = acquisition.metadata.get("column_map")
    identifiers = {"lsoa_code", "lsoa_name", "local_authority_code", "local_authority_name"}
    if not isinstance(column_map, dict) or not identifiers.issubset(frame.columns):
        raise RetrievalError("Unsupported English deprivation source schema")
    if len(set(column_map.values())) != len(column_map) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in column_map.items()
    ):
        raise RetrievalError("Deprivation source columns create ambiguous normalized names")
    measures = [
        (source, target) for source, target in column_map.items() if target not in identifiers
    ]
    if not measures or any(target not in frame for _, target in measures):
        raise RetrievalError("Deprivation source measures are missing from the retained table")
    diagnostics = []
    raw = None
    if raw_frame is not None:
        raw = raw_frame.reset_index(drop=True)
        inverse = {target: source for source, target in column_map.items()}
        code_column = inverse["lsoa_code"]
        if (
            not raw.columns.is_unique
            or code_column not in raw
            or raw[code_column].duplicated().any()
        ):
            raise RetrievalError("Retained deprivation raw data has ambiguous LSOA rows")
        indexed = raw.set_index(code_column)
        if any(code not in indexed.index for code in frame["lsoa_code"]):
            raise RetrievalError(
                "Retained deprivation raw data does not contain the selected LSOAs"
            )
        raw = indexed.loc[frame["lsoa_code"]].reset_index()
        if any(source not in raw for source, _ in measures):
            raise RetrievalError("Retained deprivation raw data is missing a source measure")
        for target in identifiers:
            source = inverse[target]
            if source not in raw or [_text(value) for value in raw[source]] != [
                _text(value) for value in frame[target]
            ]:
                raise RetrievalError(
                    "Retained deprivation raw identifiers disagree with the source table"
                )
    else:
        diagnostics.append(
            _diagnostic(
                "raw_value_reconstructed",
                "Original cell text unavailable: numeric text reconstructed from the source table, with original nonnumeric flags retained",
                "value_raw",
                len(frame) * len(measures),
            )
        )
    values = {
        name: [_code(value) for value in frame[name]]
        for name in ("lsoa_code", "local_authority_code")
    }
    dimensions = [
        _dimension(
            "lsoa_code",
            "LSOA code",
            values["lsoa_code"],
            [_text(v) for v in frame["lsoa_name"]],
            kind="geography",
            level="LSOA",
            vintage=acquisition.release.get("lsoa_geography"),
        ),
        _dimension(
            "local_authority_code",
            "Local Authority District code",
            values["local_authority_code"],
            [_text(v) for v in frame["local_authority_name"]],
            kind="geography",
            level="LAD",
            vintage=acquisition.release.get("lad_geography"),
        ),
    ]
    flags = acquisition.metadata.get("flag_columns", {})
    variables, observation_chunks = [], []
    dimension_arrays = {name: pa.array(data, type=pa.string()) for name, data in values.items()}
    for source, target in measures:
        lower = source.casefold()
        kind = (
            "decile"
            if "decile" in lower
            else "rank"
            if "rank" in lower
            else "score"
            if "score" in lower
            else "population"
            if "population" in lower
            else "measure"
        )
        unit = kind if kind in {"rank", "decile", "score"} else None
        variables.append(_variable(target, source, source, unit=unit, kind=kind))
        flag_column = flags.get(target)
        flag_values = (
            [_text(value) for value in frame[flag_column]]
            if flag_column in frame
            else [None] * len(frame)
        )
        texts = (
            [_text(value) for value in raw[source]]
            if raw is not None
            else [
                flag if flag is not None else _text(value)
                for flag, value in zip(flag_values, frame[target], strict=True)
            ]
        )
        numbers = [_number(text) for text in texts]
        if raw is not None and any(
            number != _number(value) for number, value in zip(numbers, frame[target], strict=True)
        ):
            raise RetrievalError("Retained deprivation raw values disagree with the source table")
        if raw is not None and any(
            flag is not None and flag != text for flag, text in zip(flag_values, texts, strict=True)
        ):
            raise RetrievalError("Retained deprivation raw flags disagree with the source table")
        raw_values = pa.array(texts, type=pa.string())
        numeric_values = pa.array(numbers, type=pa.float64())
        missing = pc.fill_null(
            pc.equal(pc.utf8_length(pc.utf8_trim_whitespace(raw_values)), 0), True
        )
        status = pc.if_else(
            missing, "missing", pc.if_else(pc.is_null(numeric_values), "non_numeric", "numeric")
        )
        observation_chunks.append(
            pa.table(
                {
                    **dimension_arrays,
                    "measure": pa.repeat(pa.scalar(target), len(frame)),
                    "value": numeric_values,
                    "value_raw": raw_values,
                    "unit": pa.repeat(pa.scalar(unit, type=pa.string()), len(frame)),
                    "status": status,
                    "notes": pa.array(flag_values, type=pa.string()),
                }
            )
        )
    return _build(
        acquisition,
        request,
        pa.concat_tables(observation_chunks),
        dimensions,
        variables,
        diagnostics,
    )


def normalize(
    acquisition: Acquisition, request: Request | None = None, raw_frame: pd.DataFrame | None = None
) -> Dataset:
    """Normalize supported acquisitions without changing their source table."""
    if request is not None and (
        request.provider != acquisition.provider or request.dataset_id != acquisition.dataset_id
    ):
        raise RetrievalError("Normalization request identifies a different source dataset")
    try:
        registration = get_provider(acquisition.provider)
    except ValueError as exc:
        raise RetrievalError(
            f"No dataset normalization transform for provider {acquisition.provider}"
        ) from exc
    return registration.normalize(acquisition, request=request, raw_frame=raw_frame)
