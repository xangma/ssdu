"""Pure normalization of retained statistics.gov.scot linked observation cubes."""

from __future__ import annotations

from collections import defaultdict

from ssdu.adapters.scotland import AREA, PERIOD, QB, RDFS, STATUS, UNIT
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.normalization import (
    _build,
    _checked_frame,
    _diagnostic,
    _dimension,
    _number,
    _slug,
    _text,
    _variable,
)


def normalize(acquisition: Acquisition, request: Request | None = None, raw_frame=None):
    if acquisition.provider != "scotland":
        raise RetrievalError("Scotland normalizer received a different provider")
    frame = _checked_frame(acquisition)
    structure = acquisition.metadata.get("structure", {})
    definitions = structure.get("properties", {})
    labels = acquisition.metadata.get("member_labels", {})
    if not isinstance(definitions, dict) or not definitions or "observation_uri" not in frame:
        raise RetrievalError(
            "Scotland normalization requires retained data structure and observation identities"
        )
    dimensions = []
    mapping = {}
    diagnostics = [
        _diagnostic(
            "upstream_revision_unpinned",
            "One atomic observation response with matching before/after metadata; no immutable upstream revision pin is available",
        )
    ]
    for uri, definition in sorted(definitions.items()):
        if definition["kind"] != "dimension" or uri == QB + "measureType":
            continue
        name = (
            "geography"
            if uri == AREA
            else "time"
            if uri == PERIOD
            else _slug(uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1])
        )
        if name in mapping.values():
            raise RetrievalError("Scotland dimensions have colliding normalized names")
        mapping[uri] = name
        values = [_text(value) for value in frame[uri]]
        dimensions.append(
            _dimension(
                name,
                definition.get("label") or uri,
                values,
                [labels.get(value) for value in values],
                kind="geography" if uri == AREA else "time" if uri == PERIOD else "category",
            )
        )
    measure_definitions = {
        uri: value for uri, value in definitions.items() if value["kind"] == "measure"
    }
    variables = {}
    units = defaultdict(set)
    observations = []
    for source in frame.to_dict("records"):
        selected_measure = _text(source.get(QB + "measureType"))
        if selected_measure is not None and selected_measure not in measure_definitions:
            raise RetrievalError("Scotland observation has an undeclared measureType")
        if selected_measure is not None and any(
            _text(source.get(measure)) is not None
            for measure in measure_definitions
            if measure != selected_measure
        ):
            raise RetrievalError("Scotland observation measure values conflict with measureType")
        measures = [selected_measure] if selected_measure else list(measure_definitions)
        for measure in measures:
            raw = _text(source.get(measure))
            status = _text(source.get(STATUS))
            if raw is None and status is None:
                raise RetrievalError(
                    "Scotland observation has neither a measure value nor an explicit status"
                )
            unit = _text(source.get(UNIT))
            units[measure].add(unit)
            definition = measure_definitions[measure]
            variables[measure] = _variable(measure, definition.get("label") or measure, measure)
            observations.append(
                {
                    **{name: _text(source.get(uri)) for uri, name in mapping.items()},
                    "measure": measure,
                    "value": _number(raw),
                    "value_raw": raw,
                    "unit": unit,
                    "status": status,
                    "notes": _text(source.get(RDFS + "comment")),
                }
            )
    # Empty selections keep declared variables, enabling the contract's typed zero-row table.
    for measure, definition in measure_definitions.items():
        variables.setdefault(
            measure, _variable(measure, definition.get("label") or measure, measure)
        )
    for measure, variable in variables.items():
        choices = units[measure]
        if len(choices) == 1:
            variable["unit"] = next(iter(choices))
        if None in choices or not choices:
            diagnostics.append(
                _diagnostic(
                    "unknown_unit", "No explicit source unit supplied; no unit was inferred", "unit"
                )
            )
    return _build(
        acquisition, request, observations, dimensions, list(variables.values()), diagnostics
    )
