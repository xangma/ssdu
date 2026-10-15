"""Bounded tabular-file readers without dataset-specific statistical assumptions.

CSV is retained as literal strings. A deliberately bounded CSVW subset can
declare names, primitive types, missing tokens and a primary key. Unsupported
interpretation rules fail explicitly rather than being silently discarded.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import StringIO

import pandas as pd

from ssdu.models import RetrievalError

MAX_BYTES = 25_000_000
MAX_ROWS = 100_000
MAX_COLUMNS = 1000


@dataclass
class ParsedTable:
    frame: pd.DataFrame
    source: pd.DataFrame
    columns: dict
    primary_key: list[str]
    column_map: dict[str, str]


def _dialect(value):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise RetrievalError("CSVW dialect must be an inline object")
    allowed = {
        "@id",
        "@type",
        "encoding",
        "delimiter",
        "quoteChar",
        "doubleQuote",
        "skipRows",
        "header",
        "headerRowCount",
        "skipBlankRows",
        "lineTerminators",
    }
    if set(value) - allowed:
        raise RetrievalError(f"Unsupported CSVW dialect rules: {sorted(set(value) - allowed)}")
    encoding = value.get("encoding", "utf-8").lower()
    if encoding not in {"utf-8", "utf8", "utf-8-sig"}:
        raise RetrievalError("CSV reader currently supports UTF-8 encoding only")
    delimiter = value.get("delimiter", ",")
    quote = value.get("quoteChar", '"')
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in {"\r", "\n"}:
        raise RetrievalError("CSVW delimiter must be one non-newline character")
    if not isinstance(quote, str) or len(quote) != 1 or quote in {"\r", "\n", delimiter}:
        raise RetrievalError("CSVW quoteChar must be one distinct non-newline character")
    for key in ("doubleQuote", "header", "skipBlankRows"):
        if key in value and type(value[key]) is not bool:
            raise RetrievalError(f"CSVW {key} must be boolean")
    skip = value.get("skipRows", 0)
    if type(skip) is not int or not 0 <= skip <= MAX_ROWS:
        raise RetrievalError("CSVW skipRows must be a bounded nonnegative integer")
    header = value.get("headerRowCount", 1 if value.get("header", True) else 0)
    if type(header) is not int or header != 1:
        raise RetrievalError("CSV reader currently requires exactly one header row")
    terminators = value.get("lineTerminators", ["\r\n", "\n", "\r"])
    if isinstance(terminators, str):
        terminators = [terminators]
    if not isinstance(terminators, list) or any(x not in {"\r\n", "\n", "\r"} for x in terminators):
        raise RetrievalError("CSV reader supports standard newline terminators only")
    return {
        "delimiter": delimiter,
        "quotechar": quote,
        "doublequote": value.get("doubleQuote", True),
        "skip": skip,
        "skip_blank": value.get("skipBlankRows", False),
    }


def source_frame(body: bytes, dialect=None) -> pd.DataFrame:
    """Read every source cell literally, without null or numeric inference."""
    if len(body) > MAX_BYTES:
        raise RetrievalError("CSV exceeds the 25 MB raw-file limit")
    settings = _dialect(dialect)
    try:
        reader = csv.reader(
            StringIO(body.decode("utf-8-sig"), newline=""),
            delimiter=settings["delimiter"],
            quotechar=settings["quotechar"],
            doublequote=settings["doublequote"],
            strict=True,
        )
        for _ in range(settings["skip"]):
            next(reader)
        names = next(reader)
        if not names or any(not name or len(name) > 2048 for name in names):
            raise RetrievalError("CSV requires nonempty, bounded column names")
        if len(names) != len(set(names)) or len(names) > MAX_COLUMNS:
            raise RetrievalError("CSV has duplicate column names or exceeds 1000 columns")
        rows = []
        for row in reader:
            if not row and settings["skip_blank"]:
                continue
            if len(row) != len(names):
                raise RetrievalError(
                    f"CSV record at line {reader.line_num} differs from its header"
                )
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise RetrievalError("CSV exceeds 100000 source rows; no truncation")
        if not rows:
            raise RetrievalError("CSV contains no data rows")
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise RetrievalError("Expected a nonempty UTF-8 CSV with a valid header") from exc
    return pd.DataFrame(rows, columns=names, dtype="string")


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return next((text for item in value if (text := _text(item))), None)
    if isinstance(value, dict):
        return _text(value.get("en") or value.get("en-gb") or next(iter(value.values()), None))
    return None


def _nulls(value):
    value = [value] if isinstance(value, str) else value
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise RetrievalError("CSVW null must be a string or list of strings")
    if len(value) > 100:
        raise RetrievalError("CSVW null has too many missing-value tokens")
    return value


def _convert(series, declaration):
    datatype = declaration.get("datatype", "string")
    if isinstance(datatype, dict):
        if set(datatype) - {"base", "@id"}:
            raise RetrievalError("CSVW datatype constraints/formats are not yet supported")
        datatype = datatype.get("base", "string")
    if not isinstance(datatype, str):
        raise RetrievalError("CSVW datatype must name a primitive type")
    datatype = datatype.rsplit("#", 1)[-1].removeprefix("xsd:")
    if datatype in {"string", "anyURI"}:
        return series
    kinds = {
        "integer": "Int64",
        "int": "Int64",
        "long": "Int64",
        "short": "Int64",
        "byte": "Int64",
        "nonNegativeInteger": "Int64",
        "positiveInteger": "Int64",
        "unsignedInt": "Int64",
        "unsignedShort": "Int64",
        "unsignedByte": "Int64",
        "double": "Float64",
        "float": "Float64",
        "number": "Float64",
        "boolean": "boolean",
        "decimal": "object",
        "date": "object",
        "dateTime": "object",
        "datetime": "object",
    }
    if datatype not in kinds:
        raise RetrievalError(f"Unsupported CSVW datatype: {datatype}")
    bounds = {
        "int": (-(2**31), 2**31 - 1),
        "short": (-(2**15), 2**15 - 1),
        "byte": (-128, 127),
        "unsignedInt": (0, 2**32 - 1),
        "unsignedShort": (0, 2**16 - 1),
        "unsignedByte": (0, 255),
        "nonNegativeInteger": (0, 2**63 - 1),
        "positiveInteger": (1, 2**63 - 1),
    }
    result = []
    try:
        for token in series:
            if token is pd.NA:
                result.append(pd.NA if kinds[datatype] != "object" else None)
            elif kinds[datatype] == "Int64":
                if not re.fullmatch(r"[+-]?\d+", token):
                    raise ValueError("Not an integer")
                number = int(token)
                lower, upper = bounds.get(datatype, (-(2**63), 2**63 - 1))
                if not lower <= number <= upper:
                    raise ValueError("Integer outside its declared range")
                result.append(number)
            elif kinds[datatype] == "Float64":
                if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token):
                    raise ValueError("Not a number")
                number = float(token)
                if not math.isfinite(number):
                    raise ValueError("Non-finite number")
                result.append(number)
            elif datatype == "boolean":
                if token not in {"true", "false", "1", "0"}:
                    raise ValueError("Not a boolean")
                result.append(token in {"true", "1"})
            elif datatype == "decimal":
                if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token):
                    raise ValueError("Not a decimal")
                result.append(Decimal(token))
            elif datatype == "date":
                result.append(date.fromisoformat(token))
            else:
                result.append(datetime.fromisoformat(token))
        return pd.Series(result, index=series.index, dtype=kinds[datatype])
    except (ValueError, TypeError, OverflowError) as exc:
        raise RetrievalError(f"Column {series.name!r} contains invalid {datatype} values") from exc


def parse_csv(body: bytes, *, schema=None, dialect=None, null=None) -> ParsedTable:
    """Apply explicit CSVW declarations while retaining the original cell table."""
    source = source_frame(body, dialect)
    frame = source.copy()
    columns, primary_key, mapping = {}, [], {name: name for name in source.columns}
    if schema is None:
        return ParsedTable(frame, source, columns, primary_key, mapping)
    if not isinstance(schema, dict):
        raise RetrievalError("CSVW tableSchema must resolve to an object")
    unsupported = {"foreignKeys", "rowTitles", "separator", "default", "ordered", "lang"}
    if unsupported.intersection(schema):
        raise RetrievalError("CSVW schema contains unsupported interpretation rules")
    declarations = schema.get("columns")
    if not isinstance(declarations, list) or len(declarations) != len(source.columns):
        raise RetrievalError("CSVW columns must describe every CSV column in source order")
    inherited_null = schema.get("null", [""] if null is None else null)
    names = []
    for header, declaration in zip(source.columns, declarations, strict=True):
        if not isinstance(declaration, dict):
            raise RetrievalError("Malformed CSVW column declaration")
        if {"separator", "default", "format", "ordered", "lang"}.intersection(declaration) or any(
            declaration.get(rule, False) is not False for rule in ("virtual", "suppressOutput")
        ):
            raise RetrievalError("CSVW column contains unsupported interpretation rules")
        name = declaration.get("name", header)
        if not isinstance(name, str) or not name or len(name) > 2048:
            raise RetrievalError("CSVW column names must be nonempty bounded strings")
        titles = declaration.get("titles")
        if titles is not None:
            # Names may be stable machine identifiers while titles match CSV headings.
            def title_values(value):
                if isinstance(value, str):
                    return [value]
                if isinstance(value, list):
                    return [item for sub in value for item in title_values(sub)]
                if isinstance(value, dict):
                    return [item for sub in value.values() for item in title_values(sub)]
                return []

            if header != name and header not in title_values(titles):
                raise RetrievalError(f"CSVW title/name disagrees with CSV header {header!r}")
        elif header != name:
            raise RetrievalError(f"CSVW name disagrees with CSV header {header!r}")
        missing = _nulls(declaration.get("null", inherited_null))
        values = source[header].mask(source[header].isin(missing), pd.NA)
        if "datatype" not in declaration and "datatype" in schema:
            declaration = {**declaration, "datatype": schema["datatype"]}
        required = declaration.get("required", schema.get("required", False))
        if type(required) is not bool:
            raise RetrievalError("CSVW required must be boolean")
        if required and values.isna().any():
            raise RetrievalError(f"Required CSVW column {name!r} contains nulls")
        frame[header] = _convert(values, declaration)
        columns[name] = {
            "nullable": not required,
            "missing_values": {token: "Source-declared missing value" for token in missing},
        }
        label, definition = _text(titles), _text(declaration.get("dc:description"))
        if label:
            columns[name]["label"] = label
        if definition:
            columns[name]["definition"] = definition
        mapping[header] = name
        names.append(name)
    if len(set(names)) != len(names):
        raise RetrievalError("CSVW schema creates duplicate column names")
    frame = frame.rename(columns=mapping)
    key = schema.get("primaryKey", [])
    primary_key = [key] if isinstance(key, str) else key
    if not isinstance(primary_key, list) or any(not isinstance(x, str) for x in primary_key):
        raise RetrievalError("CSVW primaryKey must name columns")
    if len(set(primary_key)) != len(primary_key) or set(primary_key) - set(names):
        raise RetrievalError("CSVW primaryKey names unknown or repeated columns")
    for name in primary_key:
        columns[name]["nullable"] = False
        if frame[name].isna().any():
            raise RetrievalError("CSVW primaryKey contains null values")
    if primary_key and frame.duplicated(primary_key).any():
        raise RetrievalError("CSVW primaryKey contains duplicate values")
    return ParsedTable(frame, source, columns, primary_key, mapping)
