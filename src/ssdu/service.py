"""Shared bounded reads, local retrieval controls and an explicitly invoked worker."""

import hashlib
import json
import re
from contextlib import contextmanager
from threading import Timer
from typing import Any

import duckdb
from pydantic import TypeAdapter

from ssdu.contracts import QuerySpec, bounded_result
from ssdu.models import RetrievalError
from ssdu.registry import Registry, dataset_key, snapshot_descriptor
from ssdu.store import Store, canonical


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
