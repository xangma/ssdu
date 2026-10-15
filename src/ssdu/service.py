"""Shared bounded reads, local retrieval controls and an explicitly invoked worker."""

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from threading import Timer
from time import monotonic
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import TypeAdapter

from ssdu.contracts import DatasetQuerySpec, QuerySpec, bounded_result
from ssdu.models import RetrievalError
from ssdu.registry import Registry, dataset_key, snapshot_descriptor
from ssdu.store import Store, canonical

MAX_DATASET_QUERY_ROWS = 5_000_000
MAX_DATASET_QUERY_BYTES = 256_000_000
DATASET_QUERY_SECONDS = 5


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class Service:
    def __init__(self, root="data", *, index_store=True):
        self.store = Store(root)
        self.registry = Registry(self.store.root)
        if index_store:
            self.registry.import_store(self.store)

    def _retrieval(self):
        from ssdu.retrieval import RetrievalService

        return RetrievalService(self.store)

    def plan_retrieval(self, spec, *, offline=False, refresh=False, budget=None):
        return bounded_result(
            self._retrieval().plan_retrieval(spec, offline=offline, refresh=refresh, budget=budget)
        )

    def get_plan(self, plan_id):
        return bounded_result(self._retrieval().get_plan(plan_id))

    def submit_plan(self, plan_id, idempotency_key):
        return bounded_result(self._retrieval().submit_plan(plan_id, idempotency_key))

    def get_job(self, job_id):
        return bounded_result(self._retrieval().get_job(job_id))

    def list_jobs(self, limit=20, offset=0):
        return bounded_result(self._retrieval().list_jobs(limit, offset))

    def cancel_job(self, job_id):
        return bounded_result(self._retrieval().cancel_job(job_id))

    def retry_job(self, job_id):
        return bounded_result(self._retrieval().retry_job(job_id))

    def run_job(self, job_id=None):
        """Run one eligible job in this process; network is confined to this worker call."""
        return bounded_result(self._retrieval().run_job(job_id))

    def search_datasets(self, query="", provider=None, limit=20, offset=0):
        return bounded_result(self.registry.search(query, provider, limit, offset))

    def describe_dataset(self, dataset_id):
        return bounded_result(self.registry.get(dataset_id))

    def list_provider_datasets(self, provider, query="", limit=20, offset=0):
        """Browse retained provider catalogue pages without network requests."""
        from ssdu.connectors import Connector

        return bounded_result(
            Connector(provider, self.store).list_datasets(
                query, limit=limit, offset=offset, offline=True
            )
        )

    def describe_provider_dataset(self, provider, dataset_id):
        """Describe cached metadata; large definitions retain compact dimension summaries."""
        from ssdu.connectors import Connector

        description = Connector(provider, self.store).describe_dataset(dataset_id, offline=True)
        try:
            return bounded_result(description)
        except ValueError as exc:
            if str(exc) != "Response exceeds the API output limit":
                raise
        # Native code lists can be large and appear both in per-dimension and
        # provider metadata. Keep them in the verified response cache; code-list
        # methods page them without expanding the public output budget.
        summary = {key: value for key, value in description.items() if key != "source_metadata"}
        summary["dimensions"] = [
            {key: value for key, value in dimension.items() if key != "source_metadata"}
            for dimension in description.get("dimensions", [])
        ]
        summary["metadata_detail"] = "summary"
        summary["omitted_fields"] = ["source_metadata", "dimensions[].source_metadata"]
        summary["metadata_note"] = (
            "Raw metadata remains cached for Python inspection; browse category codes with list_provider_dimension_values."
        )
        return bounded_result(summary)

    def list_provider_dimension_values(
        self, provider, dataset_id, dimension, search="", limit=20, offset=0
    ):
        """Return retained provider code lists, including values absent from snapshots."""
        from ssdu.connectors import Connector

        return bounded_result(
            Connector(provider, self.store).list_dimension_values(
                dataset_id, dimension, search=search, limit=limit, offset=offset, offline=True
            )
        )

    def _manifest(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
            raise ValueError("Use a full snapshot SHA-256 identifier")
        if not (self.store.snapshots / snapshot_id).is_dir():
            raise KeyError(snapshot_id)
        return self.store.verify(snapshot_id)

    @contextmanager
    def _table(self, snapshot_id):
        manifest = self._manifest(snapshot_id)
        with duckdb.connect(config={"threads": 2, "memory_limit": "256MB"}) as db:
            db.read_parquet(str(self.store.path(snapshot_id) / "data.parquet")).create_view("data")
            # SQL execution budget only: file verification and provenance are outside this timer.
            timer = Timer(5, db.interrupt)
            timer.daemon = True
            timer.start()
            try:
                yield db, manifest
            except duckdb.Error as exc:
                raise ValueError("Query failed or exceeded the local execution budget") from exc
            finally:
                timer.cancel()

    def get_provenance(self, snapshot_id):
        manifest = self._manifest(snapshot_id)
        metadata = json.loads((self.store.path(snapshot_id) / "metadata.json").read_bytes())
        version = hashlib.sha256(canonical(snapshot_descriptor(self.store, manifest))).hexdigest()
        acquisitions = self._source_acquisitions(manifest)
        return bounded_result(
            {
                "snapshot_id": manifest["product_id"],
                "dataset_id": dataset_key(manifest["provider"], manifest["dataset_id"]),
                "metadata_version": version,
                "title": manifest["title"],
                "source_url": manifest["source_url"],
                "release": manifest["release"],
                "licence": manifest["licence"],
                "citation": manifest["citation"],
                "snapshot_created_at": manifest["created_at"],
                "kind": manifest.get("kind", "selection"),
                "parent_snapshot_ids": manifest.get("parent_snapshot_ids", []),
                "product_request": manifest["request"],
                "source_acquisitions": acquisitions,
                "effective_source_request": acquisitions[0]["request"]
                if len(acquisitions) == 1
                else None,
                "content_fingerprint": manifest.get("content_fingerprint"),
                "file_hashes": {name: value["sha256"] for name, value in manifest["files"].items()},
                "quality": {
                    "validation": manifest["validation"],
                    "flag_columns": metadata.get("flag_columns", {}),
                    "comparability": metadata.get("comparability"),
                    "missing_values": metadata.get("missing_values"),
                },
                "verified": True,
                "offline": True,
            }
        )

    def _source_acquisitions(self, manifest):
        """Resolve leaf acquisitions without conflating a derived selection with provider retrieval."""
        pending, seen, acquisitions = [(manifest, 0)], set(), []
        while pending:
            current, depth = pending.pop()
            snapshot_id = current["product_id"]
            if snapshot_id in seen:
                continue
            if depth > 8 or len(seen) >= 32:
                raise RetrievalError("Snapshot lineage exceeds the provenance traversal budget")
            seen.add(snapshot_id)
            parents = current.get("parent_snapshot_ids", [])
            if parents:
                queued = {item[0]["product_id"] for item in pending}
                parent_ids = set(parents) - seen - queued
                if depth >= 8 or len(seen | queued | parent_ids) > 32:
                    raise RetrievalError("Snapshot lineage exceeds the provenance traversal budget")
                for parent_id in sorted(parent_ids, reverse=True):
                    pending.append((self._manifest(parent_id), depth + 1))
                continue
            metadata = json.loads((self.store.path(snapshot_id) / "metadata.json").read_bytes())
            acquisitions.append(
                {
                    "snapshot_id": snapshot_id,
                    "dataset_id": dataset_key(current["provider"], current["dataset_id"]),
                    "kind": current.get("kind", "selection"),
                    "request": current["request"],
                    "provider_effective_query": metadata.get("effective_query"),
                    "source_url": current["source_url"],
                    "release": current["release"],
                    "licence": current["licence"],
                    "citation": current["citation"],
                    "snapshot_created_at": current["created_at"],
                }
            )
        return sorted(acquisitions, key=lambda item: item["snapshot_id"])

    @staticmethod
    def _predicate(filters, columns):
        clauses, params = [], []
        for condition in filters:
            name, op, value = condition.column, condition.op, condition.value
            if name not in columns:
                raise ValueError("Unknown filter column")
            values = value if isinstance(value, list) else [value]
            storage_type = columns[name].upper()
            numeric = storage_type.startswith(
                (
                    "TINYINT",
                    "SMALLINT",
                    "INTEGER",
                    "BIGINT",
                    "HUGEINT",
                    "UTINYINT",
                    "USMALLINT",
                    "UINTEGER",
                    "UBIGINT",
                    "UHUGEINT",
                    "FLOAT",
                    "DOUBLE",
                    "DECIMAL",
                    "BIGNUM",
                )
            )
            boolean = storage_type == "BOOLEAN"
            for item in values:
                if item is None:
                    continue
                if numeric and (isinstance(item, bool) or not isinstance(item, (int, float))):
                    raise ValueError("Numeric filters require numeric values")
                if boolean and not isinstance(item, bool):
                    raise ValueError("Boolean filters require boolean values")
                if not numeric and not boolean and not isinstance(item, str):
                    raise ValueError("Identifier/text filters require strings")
            column = quote(name)
            if op == "in":
                present = [v for v in values if v is not None]
                parts = []
                if present:
                    parts.append(f"{column} IN ({','.join('?' for _ in present)})")
                    params.extend(present)
                if None in values:
                    parts.append(f"{column} IS NULL")
                clauses.append("(" + " OR ".join(parts) + ")")
            elif value is None:
                if op != "eq":
                    raise ValueError("Only equality supports null")
                clauses.append(f"{column} IS NULL")
            else:
                operator = {"eq": "=", "gte": ">=", "lte": "<="}[op]
                clauses.append(f"{column} {operator} ?")
                params.append(value)
        return " WHERE " + " AND ".join(clauses) if clauses else "", params

    def query_data(self, snapshot_id, query):
        query = QuerySpec.model_validate(query)
        with self._table(snapshot_id) as (db, _manifest):
            # pandas object dtype can contain integers. Query the actual retained storage types.
            schema = {row[0]: row[1] for row in db.execute("DESCRIBE data").fetchall()}
            columns = query.columns or list(schema)
            if len(set(columns)) != len(columns) or not set(columns).issubset(schema):
                raise ValueError("Unknown or duplicated output columns")
            if any(order.column not in schema for order in query.sort):
                raise ValueError("Unknown sort column")
            where, params = self._predicate(query.filters, schema)
            total = db.execute("SELECT count(*) FROM data" + where, params).fetchone()[0]
            if query.count_only:
                rows, next_offset = [{"count": total}], None
            else:
                order = [(s.column, s.descending) for s in query.sort]
                # Frozen pagination is deterministic even when user sort values tie.
                tie_columns = ["lsoa_code"] if "lsoa_code" in schema else list(schema)
                order.extend((c, False) for c in tie_columns if c not in {s[0] for s in order})
                order_sql = ",".join(quote(c) + (" DESC" if desc else " ASC") for c, desc in order)
                selection = ",".join(quote(c) for c in columns)
                cursor = db.execute(
                    f"SELECT {selection} FROM data{where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    [*params, query.limit, query.offset],
                )
                values = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
                rows = TypeAdapter(list[dict[str, Any]]).dump_python(values, mode="json")
                end = query.offset + len(rows)
                next_offset = end if end < total else None
        return bounded_result(
            {
                "rows": rows,
                "total_rows": total,
                "truncated": next_offset is not None,
                "next_offset": next_offset,
                "effective_query": query.model_dump(),
                "provenance": self.get_provenance(snapshot_id),
            }
        )

    def preview_data(self, snapshot_id, query):
        return self.query_data(snapshot_id, query)

    def _dataset(self, snapshot_id):
        # Validate the identifier before Store resolves any path. Opening verifies
        # all retained file hashes, descriptor bindings and Arrow semantics.
        manifest = self._manifest(snapshot_id)
        if "dataset_contract" not in manifest:
            raise RetrievalError(
                "Historical snapshot has no dataset contract; use migrate_dataset(snapshot_id)"
            )
        from ssdu.dataset_contract import Dataset

        root = self.store.path(snapshot_id)
        descriptor = Dataset.inspect(root)
        decoded_bytes = 0
        for table in descriptor.tables:
            if table.rows > MAX_DATASET_QUERY_ROWS:
                raise ValueError("Dataset exceeds the local query row budget")
            footer = pq.ParquetFile(root / table.path).metadata
            decoded_bytes += sum(
                footer.row_group(group).column(column).total_uncompressed_size
                for group in range(footer.num_row_groups)
                for column in range(footer.num_columns)
            )
            if decoded_bytes > MAX_DATASET_QUERY_BYTES:
                raise ValueError("Dataset exceeds the local query decoded-byte budget")
        dataset = self.store.open_dataset(snapshot_id)
        if (
            sum(dataset.to_arrow(name).nbytes for name in dataset.table_names)
            > MAX_DATASET_QUERY_BYTES
        ):
            raise ValueError("Dataset exceeds the local query decoded-byte budget")
        return dataset

    def _dataset_table(self, snapshot_id, table=None):
        dataset = self._dataset(snapshot_id)
        table = table or dataset.descriptor.profile
        if table not in ("observations", "records", "source") or table not in dataset.table_names:
            raise ValueError("Unknown dataset table")
        return dataset, table, dataset.to_arrow(table)

    @staticmethod
    def _dataset_identity(dataset):
        descriptor = dataset.descriptor
        return {
            "snapshot_id": dataset.snapshot_id,
            "dataset_id": descriptor.dataset_id,
            "contract_version": descriptor.contract_version,
            "profile": descriptor.profile,
        }

    def describe_snapshot(self, snapshot_id):
        """Describe verified contract semantics without returning unbounded code lists."""
        dataset = self._dataset(snapshot_id)
        descriptor = dataset.contract
        for dimension in descriptor["dimensions"]:
            dimension["code_count"] = len(dimension.pop("codes"))
            dimension["codes_omitted"] = True
        return bounded_result(
            {
                **self._dataset_identity(dataset),
                "default_table": dataset.descriptor.profile,
                "descriptor": descriptor,
                "descriptor_scope": "Contract summary; use snapshot dimension pagination for observed codes",
                "verification": {
                    "file_hashes": "verified",
                    "contract": "validated",
                    "offline": True,
                },
                "provenance": self.get_provenance(snapshot_id),
            }
        )

    @staticmethod
    def _dataset_operand(value, dtype):
        if value is None:
            return None
        if pa.types.is_decimal(dtype):
            if type(value) not in (str, int, float):
                raise ValueError("Decimal filters require finite decimal values")
            try:
                number = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError("Decimal filters require finite decimal values") from exc
            if not number.is_finite() or not re.fullmatch(
                r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", str(value)
            ):
                raise ValueError("Decimal filters require finite decimal values")
            return pa.scalar(number)
        if pa.types.is_integer(dtype) or pa.types.is_floating(dtype):
            if type(value) not in (int, float):
                raise ValueError("Numeric filters require numeric values")
            if pa.types.is_integer(dtype):
                return pa.scalar(Decimal(str(value)))
            return pa.scalar(value)
        if pa.types.is_boolean(dtype):
            if type(value) is not bool:
                raise ValueError("Boolean filters require boolean values")
            return pa.scalar(value)
        if not isinstance(value, str):
            raise ValueError("Identifier/text filters require strings")  # noqa: TRY004
        if pa.types.is_temporal(dtype):
            if pa.types.is_time(dtype):
                match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?", value)
                if not match:
                    raise ValueError("Time filters require an ISO time string")
                hour, minute, second = map(int, match.groups()[:3])
                if hour > 23 or minute > 59 or second > 59:
                    raise ValueError("Time filters require an ISO time string")
                nanos = ((hour * 60 + minute) * 60 + second) * 10**9 + int(
                    (match.group(4) or "").ljust(9, "0")
                )
                return pc.cast(pa.scalar(nanos, type=pa.int64()), pa.time64("ns"))
            return pc.cast(pa.scalar(value), dtype, safe=True)
        if pa.types.is_null(dtype):
            return pa.scalar(value)
        return pa.scalar(value, type=dtype)

    @staticmethod
    def _dataset_budget(started):
        if monotonic() - started > DATASET_QUERY_SECONDS:
            raise ValueError("Query exceeded the local cooperative execution budget")

    @staticmethod
    def _dataset_execution_array(column):
        """Widen only kernel inputs when Arrow lacks exact narrow scalar kernels."""
        if pa.types.is_float16(column.type):
            return pc.cast(column, pa.float32(), safe=True)
        if pa.types.is_decimal(column.type) and column.type.bit_width < 128:
            return pc.cast(
                column, pa.decimal128(column.type.precision, column.type.scale), safe=True
            )
        return column

    @classmethod
    def _dataset_execution_table(cls, table):
        for index, field in enumerate(table.schema):
            execution = cls._dataset_execution_array(table[index])
            if execution.type != field.type:
                table = table.set_column(index, field.name, execution)
        return table

    @classmethod
    def _dataset_filter(cls, table, filters, started):
        for condition in filters:
            if condition.column not in table.column_names:
                raise ValueError("Unknown filter column")
            original = table[condition.column]
            column = cls._dataset_execution_array(original)
            compared = (
                pc.cast(column, pa.decimal128(20, 0))
                if pa.types.is_integer(column.type)
                else column
            )
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            mask = None
            for value in values:
                if value is not None and pa.types.is_null(column.type):
                    # An untyped null column has no non-null values of any
                    # scalar type; equality/range matches none, IN may add null.
                    comparison = pc.is_valid(column)
                    mask = comparison if mask is None else pc.or_kleene(mask, comparison)
                    cls._dataset_budget(started)
                    continue
                operand = cls._dataset_operand(value, column.type)
                if operand is None:
                    if condition.op not in ("eq", "in"):
                        raise ValueError("Only equality supports null")
                    comparison = pc.is_null(column)
                else:
                    function = {
                        "eq": pc.equal,
                        "in": pc.equal,
                        "gte": pc.greater_equal,
                        "lte": pc.less_equal,
                    }[condition.op]
                    comparison = function(compared, operand)
                mask = comparison if mask is None else pc.or_kleene(mask, comparison)
                cls._dataset_budget(started)
            table = table.filter(pc.fill_null(mask, False))
            cls._dataset_budget(started)
        return table

    @staticmethod
    def _dataset_json_rows(table):
        # Arrow keeps nanosecond timestamps that generic datetime serialization
        # rounds to microseconds. time64[ns].as_py() also drops nanoseconds.
        def scalar(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (datetime, date, time)):
                return value.isoformat()
            return value

        for index, field in enumerate(table.schema):
            if pa.types.is_time(field.type):
                table = table.set_column(index, field.name, pc.cast(table[index], pa.string()))
        return [{name: scalar(value) for name, value in row.items()} for row in table.to_pylist()]

    def query_dataset(self, snapshot_id, query):
        """Query verified contract tables offline with exact Arrow scalar semantics.

        Inputs have explicit row/decoded-byte caps. The five-second Arrow budget
        is cooperative between kernels; it does not interrupt an active kernel.
        Hash/semantic verification occurs before that execution budget starts.
        """
        query = DatasetQuerySpec.model_validate(
            query.model_dump() if isinstance(query, QuerySpec) else query
        )
        dataset, table, retained = self._dataset_table(snapshot_id, query.table)
        started = monotonic()
        try:
            schema = retained.schema
            columns = query.columns or schema.names
            if len(set(columns)) != len(columns) or not set(columns).issubset(schema.names):
                raise ValueError("Unknown or duplicated output columns")
            if not query.count_only and len(columns) > 100:
                raise ValueError("Select at most 100 output columns")
            if any(order.column not in schema.names for order in query.sort):
                raise ValueError("Unknown sort column")
            selected = self._dataset_filter(retained, query.filters, started)
            total = selected.num_rows
            if query.count_only:
                rows, next_offset = [{"count": total}], None
            else:
                order = [
                    (item.column, "descending" if item.descending else "ascending")
                    for item in query.sort
                ]
                order.extend(
                    (name, "ascending")
                    for name in schema.names
                    if name not in {s[0] for s in order}
                )
                # Arrow's stable sort keeps identical records interchangeable,
                # and all retained columns resolve ties outside the projection.
                indices = pc.sort_indices(self._dataset_execution_table(selected), sort_keys=order)
                self._dataset_budget(started)
                page = selected.select(columns).take(indices.slice(query.offset, query.limit))
                rows = self._dataset_json_rows(page)
                end = query.offset + len(rows)
                next_offset = end if end < total else None
            self._dataset_budget(started)
            descriptor = dataset.descriptor
            specification = next(item for item in descriptor.tables if item.name == table)
            units = {
                "value_column": "unit" if table == "observations" else None,
                "variables": {item.id: item.unit for item in descriptor.variables},
                "columns": {
                    item.name: item.model_dump().get("unit")
                    for item in specification.columns
                    if item.model_dump().get("unit") is not None
                },
            }
        except (pa.ArrowException, OverflowError, TypeError) as exc:
            raise ValueError("Query operands or retained scalar types are not supported") from exc
        return bounded_result(
            {
                **self._dataset_identity(dataset),
                "table": table,
                "rows": rows,
                "total_rows": total,
                "truncated": next_offset is not None,
                "next_offset": next_offset,
                "effective_query": query.model_copy(update={"table": table}).model_dump(),
                "column_types": {"count": "int64"}
                if query.count_only
                else {
                    item.name: item.dtype for item in specification.columns if item.name in columns
                },
                "value_encoding": {
                    "decimal": "Exact decimal string",
                    "date": "ISO 8601 date string",
                    "time": "ISO 8601 time string preserving retained precision",
                    "timestamp": "ISO 8601 timestamp string preserving precision and offset",
                    "integer": "JSON integer; consumers must preserve the declared integer width",
                    "float": "JSON number",
                    "null": "JSON null",
                },
                "units": units,
                "quality": descriptor.quality.model_dump(),
                "provenance": self.get_provenance(snapshot_id),
            }
        )

    def list_snapshot_dimension_values(self, snapshot_id, dimension, search="", limit=20, offset=0):
        """Paginate declared dimension/identifier values present in one retained snapshot."""
        if (
            type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= 100
            or not 0 <= offset <= 100_000
            or not isinstance(search, str)
            or len(search) > 512
            or not isinstance(dimension, str)
            or not 1 <= len(dimension) <= 200
        ):
            raise ValueError("Invalid dimension query bounds")
        dataset, table, retained = self._dataset_table(snapshot_id)
        started = monotonic()
        try:
            descriptor = dataset.descriptor
            matches = [item for item in descriptor.dimensions if dimension == item.column]
            if not matches:
                matches = [item for item in descriptor.dimensions if dimension == item.id]
            specification = next(item for item in descriptor.tables if item.name == table)
            if matches:
                selected = matches[0]
                column, dimension_id = selected.column, selected.id
                codes = selected.codes
            elif table == "records" and (
                dimension in descriptor.primary_key
                or any(
                    item.name == dimension and item.role == "identifier"
                    for item in specification.columns
                )
            ):
                column = dimension_id = dimension
                codes = []
            else:
                raise ValueError("Unknown declared snapshot dimension or identifier")
            original = retained[column]
            values = pc.unique(self._dataset_execution_array(original))
            if values.type != original.type:
                values = pc.cast(values, original.type, safe=True)
            text_values = pc.cast(values, pa.string()).to_pylist()
            code_labels = {item.code: item.label for item in codes if item.label is not None}
            labels = pa.array(
                [code_labels.get(value, value) for value in text_values], type=pa.string()
            )
            items_table = pa.table({"value": values, "label": labels})
            self._dataset_budget(started)
            if search:
                pattern = search.lower()
                mask = pc.or_kleene(
                    pc.match_substring(pc.utf8_lower(pc.cast(values, pa.string())), pattern),
                    pc.match_substring(pc.utf8_lower(labels), pattern),
                )
                items_table = items_table.filter(pc.fill_null(mask, False))
            total = items_table.num_rows
            indices = pc.sort_indices(
                self._dataset_execution_table(items_table),
                sort_keys=[("value", "ascending"), ("label", "ascending")],
            )
            self._dataset_budget(started)
            items = self._dataset_json_rows(items_table.take(indices.slice(offset, limit)))
            self._dataset_budget(started)
        except (pa.ArrowException, OverflowError, TypeError) as exc:
            raise ValueError("Snapshot dimension scalar types are not supported") from exc
        end = offset + len(items)
        return bounded_result(
            {
                **self._dataset_identity(dataset),
                "table": table,
                "dimension": dimension_id,
                "column": column,
                "items": items,
                "total": total,
                "truncated": end < total,
                "next_offset": end if end < total else None,
                "scope": "Values present in the selected local snapshot",
                "provenance": self.get_provenance(snapshot_id),
            }
        )

    @staticmethod
    def _dimension_label(dimension, schema, metadata):
        for label, recovery in metadata.get("code_recovery", {}).items():
            if (
                isinstance(recovery, dict)
                and recovery.get("code_column") == dimension
                and label in schema
            ):
                return label
        # An explicit source column map takes priority over naming conventions.
        column_map = metadata.get("column_map", {})
        if isinstance(column_map, dict):
            for source, target in column_map.items():
                if target != dimension or not isinstance(source, str):
                    continue
                label_source = re.sub(r"\bcode\b", "name", source, flags=re.IGNORECASE)
                for raw, label in column_map.items():
                    if (
                        isinstance(raw, str)
                        and raw.casefold() == label_source.casefold()
                        and label != dimension
                        and label in schema
                    ):
                        return label
        # Known provider column conventions, accepted only when the label is actually stored.
        candidates = []
        if dimension.endswith("__code"):
            candidates.append(dimension[:-6])
        if dimension.endswith("_code"):
            candidates.append(dimension[:-5] + "_name")
        if dimension.endswith("_CODE"):
            candidates.append(dimension[:-5] + "_NAME")
        return next((label for label in candidates if label in schema), dimension)

    def list_dimension_values(self, dataset_id, dimension, search="", limit=20, offset=0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000 or len(search) > 512:
            raise ValueError("Invalid dimension query bounds")
        dataset = self.registry.get(dataset_id)
        if not dataset["snapshots"]:
            raise ValueError("Dataset has no retained snapshot")
        snapshot_id = dataset["snapshots"][0]["snapshot_id"]
        with self._table(snapshot_id) as (db, _manifest):
            schema = {row[0]: row[1] for row in db.execute("DESCRIBE data").fetchall()}
            if dimension not in schema:
                raise ValueError("Unknown dimension")
            metadata = json.loads((self.store.path(snapshot_id) / "metadata.json").read_bytes())
            label = self._dimension_label(dimension, schema, metadata)
            value_column, label_column = quote(dimension), quote(label)
            where = ""
            params = []
            if search:
                where = f" WHERE contains(lower(CAST({value_column} AS VARCHAR)), lower(?)) OR contains(lower(CAST({label_column} AS VARCHAR)), lower(?))"
                params = [search, search]
            base = (
                f"SELECT DISTINCT {value_column} AS value,{label_column} AS label FROM data{where}"
            )
            total = db.execute(f"SELECT count(*) FROM ({base})", params).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM ({base}) ORDER BY value,label LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        items = [{"value": v, "label": label} for v, label in rows]
        end = offset + len(items)
        return bounded_result(
            {
                "dimension": dimension,
                "items": items,
                "total": total,
                "truncated": end < total,
                "next_offset": end if end < total else None,
                "scope": "Values present in the selected local snapshot",
                "snapshot_id": snapshot_id,
                "provenance": self.get_provenance(snapshot_id),
            }
        )
