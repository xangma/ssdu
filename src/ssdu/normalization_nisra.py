"""Pure JSON-stat 2.0 interpretation for NISRA's PxStat cubes.

Axis and category order follow https://json-stat.org/format/. No network or
filesystem access occurs here; source codes, status tokens and metadata survive
normalization. Full downloaded cubes are bounded before local selection.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from typing import Any

import pandas as pd

from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.normalization import _build, _dimension, _number, _slug, _variable

MAX_CUBE_CELLS = 100_000
SOURCE_COLUMNS = {"cell_index", "value_raw", "status"}


def _error(message):
    raise RetrievalError(f"NISRA JSON-stat: {message}")


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        _error(f"{label} must be a list of nonempty strings")
    if len(set(value)) != len(value):
        _error(f"{label} contains duplicates")
    return value


def cube_axes(cube: Any, *, max_cells=MAX_CUBE_CELLS):
    """Validate interpretation metadata and return ordered native category codes."""
    if not isinstance(cube, dict) or cube.get("class") != "dataset" or cube.get("version") != "2.0":
        _error("expected a version 2.0 dataset")
    axes = _strings(cube.get("id"), "id")
    if not axes or SOURCE_COLUMNS.intersection(axes):
        _error("empty axes or source-column collision")
    sizes = cube.get("size")
    if (
        not isinstance(sizes, list)
        or len(sizes) != len(axes)
        or any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in sizes)
    ):
        _error("size must give a positive integer for each axis")
    cells = math.prod(sizes)
    if cells > max_cells:
        _error(f"full cube has {cells} cells; max_cube_cells={max_cells}")
    dimensions = cube.get("dimension")
    if not isinstance(dimensions, dict) or set(dimensions) != set(axes):
        _error("dimension objects must exactly match id")
    codes = {}
    for axis, size in zip(axes, sizes, strict=True):
        dim = dimensions[axis]
        if not isinstance(dim, dict) or not isinstance(dim.get("category"), dict):
            _error(f"missing category metadata for {axis}")
        category = dim["category"]
        index = category.get("index")
        labels = category.get("label", {})
        if index is None and size == 1 and isinstance(labels, dict) and len(labels) == 1:
            index = list(labels)
        if isinstance(index, dict):
            positions = list(index.values())
            if any(isinstance(x, bool) or not isinstance(x, int) for x in positions) or sorted(
                positions
            ) != list(range(size)):
                _error(f"category positions for {axis} must cover 0..size-1")
            index = [key for key, _ in sorted(index.items(), key=lambda item: item[1])]
        index = _strings(index, f"category index {axis}")
        if len(index) != size:
            _error(f"category count disagrees with size for {axis}")
        if (
            not isinstance(labels, dict)
            or set(labels) - set(index)
            or any(not isinstance(x, str) for x in labels.values())
        ):
            _error(f"invalid category labels for {axis}")
        units = category.get("unit", {})
        if (
            not isinstance(units, dict)
            or set(units) - set(index)
            or any(not isinstance(x, dict) for x in units.values())
        ):
            _error(f"invalid category units for {axis}")
        for unit in units.values():
            if "label" in unit and not isinstance(unit["label"], str):
                _error(f"unit label for {axis} must be a string")
        codes[axis] = index
    roles = cube.get("role", {})
    if not isinstance(roles, dict):
        _error("role must be an object")
    assigned = []
    for role, members in roles.items():
        _strings(members, f"role {role}")
        if set(members) - set(axes):
            _error(f"role {role} identifies an unknown axis")
        assigned.extend(members)
    if len(set(assigned)) != len(assigned) or len(roles.get("metric", [])) > 1:
        _error("overlapping roles or multiple metric axes are unsupported")
    return axes, codes, cells


def _vector(value, cells, label, *, status=False):
    if status and value is None:
        return [None] * cells
    if status and isinstance(value, str):
        return [value] * cells
    if isinstance(value, dict):
        result = [None] * cells
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"0|[1-9][0-9]*", key)
                or int(key) >= cells
            ):
                _error(f"invalid sparse {label} position")
            result[int(key)] = item
    elif isinstance(value, list) and len(value) == cells:
        result = value
    else:
        _error(f"{label} count disagrees with cube size")
    for item in result:
        if item is None:
            continue
        if status:
            if not isinstance(item, str):
                _error("status values must be strings or null")
        elif (
            isinstance(item, bool)
            or not isinstance(item, (str, int, float))
            or (isinstance(item, float) and not math.isfinite(item))
        ):
            _error("values must be finite numbers, source strings or null")
    return result


def selected_size(codes, selectors):
    if not isinstance(selectors, dict):
        _error("selectors must be an object of native codes")
    if set(selectors) - set(codes):
        _error("selectors identify an unknown axis")
    for axis, values in selectors.items():
        _strings(values, f"selector {axis}")
        if not values or set(values) - set(codes[axis]):
            _error(f"selector {axis} contains an unknown or empty category selection")
    return math.prod(len(selectors.get(axis, choices)) for axis, choices in codes.items())


def frame_from_cube(cube, selectors, *, max_cells=MAX_CUBE_CELLS):
    """Expand every selected cube cell, including sparse missing observations."""
    axes, codes, cells = cube_axes(cube, max_cells=max_cells)
    expected = selected_size(codes, selectors)
    values = _vector(cube.get("value"), cells, "value")
    statuses = _vector(cube.get("status"), cells, "status", status=True)
    chosen = {axis: set(values) for axis, values in selectors.items()}
    rows = []
    for position, combination in enumerate(itertools.product(*(codes[axis] for axis in axes))):
        row = dict(zip(axes, combination, strict=True))
        if any(row[axis] not in values for axis, values in chosen.items()):
            continue
        value = values[position]
        rows.append(
            {
                **row,
                "cell_index": position,
                "value_raw": value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, allow_nan=False)
                if value is not None
                else None,
                "status": statuses[position],
            }
        )
    if len(rows) != expected:
        _error("selected cube cardinality mismatch")
    frame = pd.DataFrame(rows, columns=[*axes, "cell_index", "value_raw", "status"])
    for column in [*axes, "value_raw", "status"]:
        frame[column] = frame[column].astype("string")
    return frame


def _notes(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    _error("notes must be strings or lists of strings")


def normalize(acquisition: Acquisition, request: Request | None = None, raw_frame=None):
    """Normalize a retained NISRA table without contacting the provider."""
    if acquisition.provider != "nisra":
        _error("normalizer received a different provider")
    cube = acquisition.metadata.get("cube_metadata")
    axes, codes, cells = cube_axes(cube)
    frame = acquisition.frame.reset_index(drop=True)
    if set(frame.columns) != {*axes, *SOURCE_COLUMNS}:
        _error("source table columns disagree with retained cube metadata")
    if not frame.columns.is_unique or frame["cell_index"].duplicated().any():
        _error("duplicate source columns or cube cells")
    selectors = acquisition.metadata.get("selectors", {})
    expected = selected_size(codes, selectors)
    if len(frame) != expected:
        _error("source row count disagrees with selected cube")
    sizes = cube["size"]
    strides = [math.prod(sizes[position + 1 :]) for position in range(len(axes))]
    for row in frame.to_dict("records"):
        position = row["cell_index"]
        if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < cells:
            _error("source cell_index is outside the cube")
        for axis, stride, size in zip(axes, strides, sizes, strict=True):
            expected_code = codes[axis][position // stride % size]
            if (
                not isinstance(row[axis], str)
                or row[axis] != expected_code
                or expected_code not in selectors.get(axis, codes[axis])
            ):
                _error("source category code disagrees with row-major cube position")
        for column in ["value_raw", "status"]:
            value = row[column]
            if not isinstance(value, str) and not pd.isna(value):
                _error(f"source {column} must contain strings or null")
    roles = cube.get("role", {})
    metric = next(iter(roles.get("metric", [])), None)
    mapped = {axis: _slug(axis) for axis in axes if axis != metric}
    if len(set(mapped.values())) != len(mapped):
        _error("axis names collide after normalization")
    dimensions = []
    for axis, column in mapped.items():
        dim = cube["dimension"][axis]
        native = frame[axis].tolist()
        labels = dim["category"].get("label", {})
        # NISRA's national NI dimension omits role.geo in the published WDTHS
        # cube. This exact native code/label mapping is explicit provider data.
        national = (
            axis == "NI"
            and codes[axis] == ["N92000002"]
            and labels.get("N92000002") == "Northern Ireland"
        )
        kind = (
            "time"
            if axis in roles.get("time", [])
            else "geography"
            if axis in roles.get("geo", []) or national
            else "category"
        )
        dimensions.append(
            _dimension(
                column,
                dim.get("label", axis),
                native,
                [labels.get(x) for x in native],
                kind=kind,
                level="country" if national else None,
            )
        )
    metric_meta = cube["dimension"][metric] if metric else {}
    metric_codes = sorted(set(frame[metric])) if metric else ["value"]
    units = metric_meta.get("category", {}).get("unit", {})
    labels = metric_meta.get("category", {}).get("label", {})
    variables = [
        _variable(code, labels.get(code, code), "value_raw", unit=units.get(code, {}).get("label"))
        for code in metric_codes
    ]
    observations = []
    global_notes = _notes(cube.get("note"))
    for row in frame.to_dict("records"):
        identity = row[metric] if metric else "value"
        notes = list(global_notes)
        for axis in axes:
            dim = cube["dimension"][axis]
            notes.extend(f"{axis}: {note}" for note in _notes(dim.get("note")))
            category_notes = dim["category"].get("note", {})
            if not isinstance(category_notes, dict):
                _error("category notes must map native codes to notes")
            notes.extend(
                f"{axis}={row[axis]}: {note}" for note in _notes(category_notes.get(row[axis]))
            )
        value = row["value_raw"]
        value = None if pd.isna(value) else value
        status = row["status"]
        status = None if pd.isna(status) else status
        observations.append(
            {
                **{column: row[axis] for axis, column in mapped.items()},
                "measure": identity,
                "value": _number(value),
                "value_raw": value,
                "status": status,
                "unit": units.get(identity, {}).get("label"),
                "notes": "\n".join(notes) or None,
            }
        )
    return _build(acquisition, request, observations, dimensions, variables, [])
